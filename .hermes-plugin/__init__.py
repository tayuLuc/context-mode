"""
Hermes Agent plugin for Context Mode integration.

Five hooks:
  pre_tool_call       - Block high-output terminal commands, redirect to ctx_execute
  transform_tool_result - Sandbox large outputs to files, return compact summaries
  pre_llm_call        - Inject routing rules on first turn of each session
  on_session_start    - Initialize metrics tracking
  on_session_end      - Persist session metrics to SQLite

Installs via: cp -r .hermes-plugin ~/.hermes/plugins/hermes-context-mode
Then add to config.yaml: plugins.enabled: [hermes-context-mode]
"""

from __future__ import annotations

import json
import logging
import os
import re
import sqlite3
from collections import Counter
from datetime import datetime
from pathlib import Path
from textwrap import dedent
from typing import Optional

logger = logging.getLogger("hermes-context-mode")

# ── Constants ────────────────────────────────────────────────────────────

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
PLUGIN_DIR = HERMES_HOME / "plugins" / "hermes-context-mode"
METRICS_DB = PLUGIN_DIR / "metrics.db"
SANDBOX_DIR = PLUGIN_DIR / "sandbox"

SANDBOX_THRESHOLD = 3 * 1024  # 3KB

# Commands that pass through without blocking
ALLOWED_COMMANDS = [
    "git ", "mkdir", "rm ", "mv ", "cp ", "touch", "chmod",
    "ls ", "pwd", "cd ", "echo ", "cat ", "head ", "tail ",
    "npm install ", "pip install ", "pip3 install ",
    "which ", "whoami", "hostname", "uname", "date", "env",
    "hermes ", "brew ",
]

# High-output commands to block (redirect to ctx_execute)
BLOCKED_HIGH_OUTPUT = re.compile(
    r"\b(curl|wget|docker\s+(build|compose\s+up)|"
    r"make\b|cmake\b|gradle\b|mvn\b|cargo\s+(build|test|run|check)|"
    r"npx\b|npm\s+(run|start|test)|"
    r"playwright\s+(open|codegen|install)|"
    r"kubectl\s+(get|logs|describe|apply))\b"
)

# Regex for inline HTTP in execute_code
BLOCKED_INLINE_HTTP = re.compile(
    r"\b(fetch\s*\(\s*['\"]http|"
    r"requests\.(get|post|put|delete|patch)\s*\(|"
    r"http\.(get|post|request)\s*\(|"
    r"urllib\.request\.urlopen\s*\()"
)

# Tools that should NEVER be sandboxed
NEVER_SANDBOX = {"write_file", "patch", "text_to_speech", "send_message", "vision_analyze"}

# Tools eligible for sandboxing
SANDBOX_TOOLS = {"terminal", "read_file", "browser_snapshot", "browser_console",
                 "browser_vision", "web_extract", "web_search", "execute_code"}

# ── Guidance block (injected once per session — single source: upstream routing-block.mjs) ──

# Synchronised from: hooks/routing-block.mjs (upstream)
GUIDANCE = dedent("""\
    <context_window_protection>
      <priority_instructions>
        Raw tool output floods context window. MUST use context-mode MCP tools. Keep raw data in sandbox.
      </priority_instructions>

      <tool_selection_hierarchy>
        0. MEMORY: ctx_search(sort: "timeline")
           - After resume, check prior context before asking user.
        1. GATHER: ctx_batch_execute(commands, queries)
           - Primary research tool. Runs commands, auto-indexes, searches. ONE call replaces many steps.
           - Each command: {label: "section header", command: "shell command"}
           - label becomes FTS5 chunk title — descriptive labels improve search.
        2. FOLLOW-UP: ctx_search(queries: ["q1", "q2", ...])
           - All follow-up questions. ONE call, many queries (default relevance mode).
        3. PROCESSING: ctx_execute(language, code) | ctx_execute_file(path, language, code)
           - API calls, log analysis, data processing.
      </tool_selection_hierarchy>

      <forbidden_actions>
        - NO Bash for commands producing >20 lines output.
        - NO Read for analysis — use ctx_execute_file. Read IS correct for files you intend to Edit.
        - NO WebFetch — use ctx_fetch_and_index.
        - Bash ONLY for git/mkdir/rm/mv/navigation.
        - NO ctx_execute or ctx_execute_file for file creation/modification.
          ctx_execute is for analysis, processing, computation only.
      </forbidden_actions>

      <file_writing_policy>
        ALWAYS use native Write/Edit tools for file creation/modification.
        NEVER use ctx_execute, ctx_execute_file, or Bash to write files.
        Applies to all file types: code, configs, plans, specs, YAML, JSON, markdown.
      </file_writing_policy>

      <output_constraints>
        <communication_style>
          Terse like caveman. Technical substance exact. Only fluff die.
          Use fragments when clear. Short synonyms (fix not "implement a solution for").
          Technical terms exact. Code blocks unchanged.
          Auto-expand for: security warnings, irreversible actions, user confusion.
        </communication_style>
        <artifact_policy>
          Write artifacts (code, configs, PRDs) to FILES. NEVER inline.
          Return only: file path + 1-line description.
        </artifact_policy>
        <response_format>
          Concise summary:
          - Actions taken (2-3 bullets)
          - File paths created/modified
          - Key findings
        </response_format>
      </output_constraints>

      <session_continuity>
        Skills, roles, and decisions set during this session remain active until the user revokes them.
        Do not drop behavioral directives as context grows.
      </session_continuity>

      <ctx_commands>
        "ctx stats" | "ctx-stats" | "/ctx-stats" | context savings question
        → Call stats MCP tool, display full output verbatim.

        "ctx doctor" | "ctx-doctor" | "/ctx-doctor" | diagnose context-mode
        → Call doctor MCP tool, run returned shell command, display as checklist.

        "ctx upgrade" | "ctx-upgrade" | "/ctx-upgrade" | update context-mode
        → Call upgrade MCP tool, run returned shell command, display as checklist.

        "ctx purge" | "ctx-purge" | "/ctx-purge" | wipe/reset knowledge base
        → Call purge MCP tool with confirm: true. Warn: irreversible.

        After /clear or /compact: knowledge base preserved. Tell user: "context-mode knowledge base preserved. Use `ctx purge` to start fresh."
      </ctx_commands>
    </context_window_protection>
""")

# ── Module-level state ─────────────────────────────────────────────────

SESSION_GUIDANCE_SHOWN: dict[str, bool] = {}  # session_id -> guidance injected
_session_stats: dict[str, dict] = {}           # session_id -> metrics

_GUIDANCE_CAP = 1000  # max entries before eviction


# ── SQLite metrics ─────────────────────────────────────────────────────

def _ensure_db() -> sqlite3.Connection:
    PLUGIN_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(METRICS_DB))
    conn.execute("""
        CREATE TABLE IF NOT EXISTS session_metrics (
            session_id TEXT PRIMARY KEY,
            platform TEXT, model TEXT,
            started TEXT, ended TEXT,
            tool_calls INTEGER DEFAULT 0,
            bytes_saved INTEGER DEFAULT 0,
            tools_saved TEXT DEFAULT '{}',
            blocks INTEGER DEFAULT 0
        )
    """)
    conn.execute("""
        CREATE TABLE IF NOT EXISTS tool_savings (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT, tool_name TEXT,
            original_bytes INTEGER, saved_bytes INTEGER,
            sandbox_path TEXT, ts TEXT
        )
    """)
    conn.commit()
    return conn


def _record_saving(session_id: str, tool_name: str,
                   original_bytes: int, saved_bytes: int, path: str = "") -> None:
    try:
        conn = _ensure_db()
        conn.execute(
            "INSERT INTO tool_savings VALUES (NULL, ?, ?, ?, ?, ?, ?)",
            (session_id, tool_name, original_bytes, saved_bytes, path,
             datetime.now().isoformat()),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("Metrics save failed: %s", e)


def _update_session(session_id: str, **kw) -> None:
    stats = _session_stats.get(session_id)
    if stats:
        stats.update(kw)


# ── Helpers ────────────────────────────────────────────────────────────

def _fire_hook(event: str, payload: dict) -> None:
    """Forward event to context-mode SessionDB via CLI hook."""
    try:
        proc = subprocess.run(
            ["context-mode", "hook", "hermes", event],
            input=json.dumps(payload),
            capture_output=True, text=True, timeout=10,
        )
        if proc.returncode != 0:
            logger.debug("_fire_hook %s stderr: %s", event, proc.stderr[:200])
    except Exception as e:
        logger.debug("_fire_hook %s error: %s", event, e)


def _is_allowed(stripped: str) -> bool:
    return any(stripped.startswith(a) for a in ALLOWED_COMMANDS)


def _count_bytes(obj) -> int:
    """Recursively count bytes in a nested structure."""
    if isinstance(obj, str):
        return len(obj.encode("utf-8"))
    if isinstance(obj, dict):
        return sum(_count_bytes(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return sum(_count_bytes(item) for item in obj)
    return len(str(obj).encode("utf-8"))


# ── Hook: on_session_start ─────────────────────────────────────────────

def on_session_start(session_id: str, model: str, platform: str, **kwargs) -> None:
    _session_stats[session_id] = {
        "tool_calls": 0, "bytes_saved": 0, "blocks": 0,
        "tools_saved": Counter(),
        "model": model, "platform": platform,
        "started": datetime.now().isoformat(),
    }
    # Clean up guidance tracker for recycled session IDs
    SESSION_GUIDANCE_SHOWN.pop(session_id, None)
    # Enforce cap to prevent unbounded growth
    if len(SESSION_GUIDANCE_SHOWN) > _GUIDANCE_CAP:
        SESSION_GUIDANCE_SHOWN.clear()
    logger.info("Session %s started: %s/%s", session_id[:8], platform, model)
    _fire_hook("sessionstart", {
        "session_id": session_id,
        "platform": platform,
        "model": model,
    })


# ── Hook: on_session_end ───────────────────────────────────────────────

def on_session_end(session_id: str, completed: bool, interrupted: bool, **kwargs) -> None:
    stats = _session_stats.pop(session_id, None)
    if not stats:
        return

    try:
        conn = _ensure_db()
        conn.execute(
            "INSERT OR REPLACE INTO session_metrics VALUES (?,?,?,?,?,?,?,?,?)",
            (
                session_id, stats["platform"], stats["model"],
                stats["started"], datetime.now().isoformat(),
                stats["tool_calls"], stats["bytes_saved"],
                json.dumps(dict(stats["tools_saved"])), stats["blocks"],
            ),
        )
        conn.commit()
        conn.close()
    except Exception as e:
        logger.debug("Session metrics save failed: %s", e)

    status = "completed" if completed else ("interrupted" if interrupted else "failed")
    saved_kb = stats["bytes_saved"] / 1024
    if saved_kb > 0 or stats["blocks"] > 0:
        logger.info(
            "Session %s %s: saved %.1fKB, %d blocks across %d tool calls",
            session_id[:8], status, saved_kb, stats["blocks"], stats["tool_calls"],
        )
    _fire_hook("sessionend", {
        "session_id": session_id,
        "status": status,
        "tool_calls": stats["tool_calls"],
        "bytes_saved": stats["bytes_saved"],
        "blocks": stats["blocks"],
    })


# ── Hook: pre_tool_call (PROACTIVE — block before execution) ───────────

def pre_tool_call(*, tool_name: str, args: dict, task_id: str,
                  session_id: str = "", **_kwargs) -> Optional[dict]:
    """Block high-output terminal commands; redirect to ctx_execute (MCP)."""
    if tool_name != "terminal":
        return None

    command = args.get("command", "")
    if not isinstance(command, str) or not command.strip():
        return None

    stripped = command.strip()

    # Allowlist
    if _is_allowed(stripped):
        return None

    # Track call count
    _update_session(session_id, tool_calls=_session_stats.get(session_id, {}).get("tool_calls", 0) + 1)

    # Block: known high-output commands
    if BLOCKED_HIGH_OUTPUT.search(stripped):
        _update_session(session_id, blocks=_session_stats.get(session_id, {}).get("blocks", 0) + 1)
        return {
            "action": "block",
            "message": (
                "context-mode: High-output command blocked. "
                "Use ctx_execute (context-mode MCP tool) to run it in the sandbox."
            ),
        }

    # Block: inline HTTP in execute_code
    if tool_name == "terminal" and BLOCKED_INLINE_HTTP.search(stripped):
        captures = BLOCKED_INLINE_HTTP.search(stripped)
        url_match = re.search(r"https?://[^\s\"'()]+", stripped)
        url = url_match.group(0) if url_match else ""
        return {
            "action": "block",
            "message": (
                f"context-mode: Inline HTTP blocked. Use ctx_fetch_and_index "
                f"to fetch and index \"{url}\" via context-mode MCP."
            ),
        }

    return None


# ── Hook: transform_tool_result (REACTIVE — sandbox large outputs) ─────

def transform_tool_result(*, tool_name: str, args: dict, result: str,
                          session_id: str = "", task_id: str = "",
                          **_kwargs) -> Optional[str]:
    """Sandbox large outputs; return compact summary."""
    if tool_name in NEVER_SANDBOX:
        return None
    if tool_name not in SANDBOX_TOOLS:
        return None
    if not isinstance(result, str) or len(result) <= SANDBOX_THRESHOLD:
        return None

    # Track this tool call
    _update_session(session_id, tool_calls=_session_stats.get(session_id, {}).get("tool_calls", 0) + 1)

    # Unwrap JSON
    raw_content = result
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict):
            content_field = next(
                (v for k, v in parsed.items()
                 if k in ("content", "output", "result") and isinstance(v, str)),
                None,
            )
            if content_field:
                raw_content = content_field
    except json.JSONDecodeError:
        pass

    # Ensure sandbox directory exists
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)

    # Write to sandbox
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = tool_name.replace("/", "_")
    fname = f"{ts}_{safe_name}_{task_id[:8] if task_id else 'na'}.txt"
    fpath = SANDBOX_DIR / fname
    fpath.write_text(raw_content, encoding="utf-8")

    # Original bytes: what would have been in context
    original_bytes = _count_bytes(raw_content)

    # Compact summary
    line_count = raw_content.count("\n") + 1
    preview = raw_content[:200].strip()
    summary = f"""<sandboxed_output tool="{tool_name}" file="{fpath}" lines="{line_count}" saved="{original_bytes}B">
  Output >3KB — written to sandbox file.
  Use `read_file(path="{fpath}")` to view full output.
  Preview: {preview}
</sandboxed_output>"""
    # Saved bytes: how many bytes DIDN'T go into context
    summary_bytes = _count_bytes(summary)
    saved = original_bytes - summary_bytes

    _update_session(session_id, bytes_saved=_session_stats.get(session_id, {}).get("bytes_saved", 0) + saved)
    stats = _session_stats.get(session_id)
    if stats:
        stats["tools_saved"][tool_name] = stats["tools_saved"].get(tool_name, 0) + saved

    _record_saving(session_id, tool_name, original_bytes, summary_bytes, str(fpath))
    _fire_hook("posttooluse", {
        "session_id": session_id,
        "tool_name": tool_name,
        "blocked": False,
        "saved_bytes": saved,
        "sandbox_path": str(fpath),
        "sandboxed": True,
    })

    return summary



def pre_llm_call(*, session_id: str, user_message: str,
                 is_first_turn: bool, **kwargs) -> Optional[dict]:
    """Inject context optimization instructions on first turn."""
    if not is_first_turn:
        return None
    if session_id in SESSION_GUIDANCE_SHOWN:
        return None

    SESSION_GUIDANCE_SHOWN[session_id] = True

    # Enforce cap (belt-and-suspenders with on_session_start)
    if len(SESSION_GUIDANCE_SHOWN) > _GUIDANCE_CAP:
        SESSION_GUIDANCE_SHOWN.clear()
        SESSION_GUIDANCE_SHOWN[session_id] = True

    return {"context": GUIDANCE}


# ── Plugin registration ────────────────────────────────────────────────

def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("transform_tool_result", transform_tool_result)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_end", on_session_end)
    logger.info("hermes-context-mode registered (5 hooks)")
