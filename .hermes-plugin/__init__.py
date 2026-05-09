"""
Hermes Agent plugin for Context Mode integration.

Five hooks:
  pre_tool_call         - Block high-output terminal commands, redirect to ctx_execute
  transform_tool_result - Sandbox large outputs (>3KB), return compact summary
  pre_llm_call          - Inject context optimization guidance on first turn
  on_session_start      - Initialize in-memory stats tracking
  on_session_end        - Log session stats summary

Three slash commands:
  /ctx-stats            - Show in-memory context savings stats
  /ctx-doctor           - Run plugin diagnostics
  /ctx-purge            - Clear sandbox and reset in-memory stats
"""

from __future__ import annotations

import json
import logging
import shutil
from collections import Counter
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hermes-context-mode")

# ── Constants ────────────────────────────────────────────────────────────

PLUGIN_DIR = Path(__file__).parent.resolve()
SANDBOX_DIR = PLUGIN_DIR / "sandbox"

# Tools that should never be sandboxed
NEVER_SANDBOX = {"read_file"}

# Tools eligible for sandboxing
SANDBOX_TOOLS = {"read_file", "execute_code", "terminal",
                 "browser_snapshot", "browser_console", "browser_vision"}

SANDBOX_THRESHOLD = 3072  # 3KB

# Commands exempt from terminal → ctx_execute redirect
ALLOWED_COMMANDS = (
    "cd ", "ls ", "pwd ",
    "git ",
    "which ", "type ",
    "echo ",
    "source ", "export ", "eval ",
    "python3 -c \"import",
    "node -e \"import",
)

_GUIDANCE_CAP = 100

# In-memory session stats (reset each session, no persistence)
_session_stats: dict[str, dict] = {}
SESSION_GUIDANCE_SHOWN: dict[str, bool] = {}


# ── Guidance block (injected once per session) ──────────────────────────

GUIDANCE = """\
<context_optimization>
  IMPORTANT: Context Mode is active. When a tool returns a large result
  (>3KB), it will be intercepted and sandboxed. You'll see a compact
  summary with a file path. Use read_file(path="...") to retrieve the
  full content when you actually need it.

  This saves your context window from being flooded with raw output.
</context_optimization>"""


# ── Helpers ────────────────────────────────────────────────────────────


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
        "tool_calls": 0,
        "bytes_saved": 0,
        "tools_saved": Counter(),
        "model": model,
        "platform": platform,
        "started": datetime.now().isoformat(),
    }
    SESSION_GUIDANCE_SHOWN.pop(session_id, None)
    if len(SESSION_GUIDANCE_SHOWN) > _GUIDANCE_CAP:
        SESSION_GUIDANCE_SHOWN.clear()
    logger.info("Session %s started: %s/%s", session_id[:8], platform, model)


# ── Hook: on_session_end ───────────────────────────────────────────────


def on_session_end(session_id: str, completed: bool, interrupted: bool, **kwargs) -> None:
    stats = _session_stats.pop(session_id, None)
    if not stats:
        return

    saved_kb = stats["bytes_saved"] / 1024
    if saved_kb > 0 or stats["tool_calls"] > 0:
        logger.info(
            "Session %s ended: %d calls, saved %.1fKB across %d tools",
            session_id[:8], stats["tool_calls"], saved_kb, len(stats["tools_saved"]),
        )


# ── Hook: pre_tool_call (PROACTIVE — block before execution) ───────────


def pre_tool_call(*, tool_name: str, args: dict, task_id: str,
                  session_id: str = "", **_kwargs) -> Optional[dict]:
    """Redirect bash curl/wget/find/grep to ctx_execute before they run."""
    if tool_name != "terminal":
        return None

    cmd = args.get("command", "").strip()
    if not cmd:
        return None

    if _is_allowed(cmd):
        return None

    stats = _session_stats.get(session_id)
    if stats:
        stats["tool_calls"] += 1

    return {
        "tool_name": "ctx_execute",
        "args": {
            "language": "shell",
            "code": cmd,
            "timeout": args.get("timeout", 30),
            "intent": f"terminal: {cmd}",
        },
    }


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

    # Unwrap JSON result to extract content
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

    # Write to sandbox file
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    safe_name = tool_name.replace("/", "_")
    fname = f"{ts}_{safe_name}_{task_id[:8] if task_id else 'na'}.txt"
    fpath = SANDBOX_DIR / fname
    fpath.write_text(raw_content, encoding="utf-8")

    # Count bytes saved
    original_bytes = _count_bytes(raw_content)
    line_count = raw_content.count("\n") + 1
    preview = raw_content[:200].strip()
    summary = f"""<sandboxed_output tool=\"{tool_name}\" file=\"{fpath}\" lines=\"{line_count}\" saved=\"{original_bytes}B\">
  Output >3KB — written to sandbox file.
  Use `read_file(path=\"{fpath}\")` to view full output.
  Preview: {preview}
</sandboxed_output>"""
    summary_bytes = _count_bytes(summary)
    saved = original_bytes - summary_bytes

    # Update in-memory stats
    stats = _session_stats.get(session_id)
    if stats:
        stats["tool_calls"] += 1
        stats["bytes_saved"] += saved
        stats["tools_saved"][tool_name] += saved

    return summary


# ── Hook: pre_llm_call ─────────────────────────────────────────────────


def pre_llm_call(*, session_id: str, user_message: str,
                 is_first_turn: bool, **kwargs) -> Optional[dict]:
    """Inject context optimization instructions on first turn."""
    if not is_first_turn:
        return None
    if session_id in SESSION_GUIDANCE_SHOWN:
        return None

    SESSION_GUIDANCE_SHOWN[session_id] = True
    if len(SESSION_GUIDANCE_SHOWN) > _GUIDANCE_CAP:
        SESSION_GUIDANCE_SHOWN.clear()
        SESSION_GUIDANCE_SHOWN[session_id] = True

    return {"context": GUIDANCE}


# ── Slash command handlers ──────────────────────────────────────────────


async def _cmd_ctx_stats(raw_args: str) -> str:
    """Handler for /ctx-stats — read in-memory stats."""
    total = len(_session_stats)

    if not _session_stats:
        return "context-mode stats\n  No active sessions."

    lines = ["context-mode stats", f"  Sessions tracked: {total} active"]

    for sid, stats in list(_session_stats.items())[:5]:
        by_tool = dict(stats.get("tools_saved", {}))
        tool_summary = " ".join(
            f"{t}={v/1024:.0f}KB" for t, v in sorted(by_tool.items(),
            key=lambda x: -x[1])[:4]
        )
        lines.append(
            f"  {sid[:8]}: calls={stats['tool_calls']} "
            f"saved={stats['bytes_saved']/1024:.1f}KB"
        )
        if tool_summary:
            lines.append(f"    {tool_summary}")

    return "\n".join(lines)


async def _cmd_ctx_doctor(raw_args: str) -> str:
    """Handler for /ctx-doctor — run diagnostics."""
    checks = []

    plugin_dir = PLUGIN_DIR.exists()
    checks.append(f"{'✓' if plugin_dir else '✗'} Plugin dir: {PLUGIN_DIR}")

    if SANDBOX_DIR.exists():
        n_files = len(list(SANDBOX_DIR.iterdir()))
        checks.append(f"✓ Sandbox: {n_files} files ({SANDBOX_DIR})")
    else:
        checks.append("✓ Sandbox dir: not yet created (will be on first sandbox)")

    active = len(_session_stats)
    checks.append(f"✓ Active sessions tracked: {active}")

    import shutil as _su
    mcp_bin = _su.which("context-mode")
    checks.append(f"{'✓' if mcp_bin else '✗'} MCP server binary found"
                  + (f" at {mcp_bin}" if mcp_bin else ""))

    return "\n".join(checks)


async def _cmd_ctx_purge(raw_args: str) -> str:
    """Handler for /ctx-purge — clear sandbox and reset in-memory stats."""
    confirm = raw_args.strip().lower()
    if confirm != "yes":
        return (
            "⚠️ This will DELETE all sandbox files and reset in-memory stats.\n"
            "Run `/ctx-purge yes` to confirm."
        )

    deleted = []

    if SANDBOX_DIR.exists():
        shutil.rmtree(SANDBOX_DIR)
        deleted.append(f"sandbox/ ({SANDBOX_DIR})")

    _session_stats.clear()
    SESSION_GUIDANCE_SHOWN.clear()
    deleted.append("in-memory stats")

    return f"Purged: {', '.join(deleted)}."


# ── Plugin registration ────────────────────────────────────────────────


def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("transform_tool_result", transform_tool_result)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("on_session_start", on_session_start)
    ctx.register_hook("on_session_end", on_session_end)
    ctx.register_command("ctx_stats", _cmd_ctx_stats)
    ctx.register_command("ctx_doctor", _cmd_ctx_doctor)
    ctx.register_command("ctx_purge", _cmd_ctx_purge)
    logger.info("hermes-context-mode registered (5 hooks, 3 commands)")
