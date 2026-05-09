"""
Hermes Agent plugin for Context Mode integration.

Four hooks:
  pre_tool_call         - Block high-output terminal commands
  transform_tool_result - Sandbox large outputs to files
  pre_llm_call          - Inject guidance on first turn
  on_session_start      - Track session lifecycle

Analytics forwarded to context-mode SessionDB via:
  context-mode hook hermes posttooluse  (stdin JSON)
  context-mode hook hermes sessionstart (stdin JSON)

Installs via: cp -r .hermes-plugin ~/.hermes/plugins/hermes-context-mode
Then add to config.yaml: plugins.enabled: [hermes-context-mode]
"""

from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime
from pathlib import Path
from typing import Optional

logger = logging.getLogger("hermes-context-mode")

# ── Constants ────────────────────────────────────────────

HERMES_HOME = Path(os.environ.get("HERMES_HOME", Path.home() / ".hermes"))
PLUGIN_DIR = HERMES_HOME / "plugins" / "hermes-context-mode"
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
INLINE_HTTP = re.compile(
    r"\b(fetch\s*\([\"']https?://|"
    r"curl\s+|wget\s+|"
    r"requests\.(get|post|put|delete)\s*\(|"
    r"urllib\.request|"
    r"httpx\.(get|post|put|delete)\s*\()"
)

# Tools that should NEVER be sandboxed
NEVER_SANDBOX = {"ctx_execute", "ctx_execute_file", "ctx_batch_execute"}

# ── Guidance block (injected once per session) ──────────

GUIDANCE = (
    "Before running a command, evaluate if its output will exceed ~3 KB. "
    "If yes, use ctx_execute(language=\"shell\", code=\"...\") instead of terminal. "
    "For data analysis (filtering, counting, searching), use ctx_execute with Python/JavaScript. "
    "fetch/curl/wget are blocked in shell — use ctx_execute or ctx_fetch_and_index instead."
)

# ── Module-level state ──────────────────────────────────

_session_guidance_injected: set[str] = set()
_session_id: str = ""


# ── Helpers ────────────────────────────────────────────

def _is_allowed(stripped: str) -> bool:
    return any(stripped.startswith(a) for a in ALLOWED_COMMANDS)


def _count_bytes(obj) -> int:
    """Recursively count bytes in a nested structure."""
    if isinstance(obj, str):
        return len(obj.encode("utf-8"))
    if isinstance(obj, bytes):
        return len(obj)
    if isinstance(obj, dict):
        return sum(_count_bytes(v) for v in obj.values())
    if isinstance(obj, (list, tuple)):
        return sum(_count_bytes(i) for i in obj)
    return 0


def _fire_hook(event: str, payload: dict) -> None:
    """Fire a context-mode CLI hook with JSON payload on stdin.

    This forwards events to context-mode's SessionDB for analytics
    and ctx stats reporting. Errors are non-fatal.
    """
    try:
        subprocess.run(
            ["context-mode", "hook", "hermes", event],
            input=json.dumps(payload),
            capture_output=True,
            text=True,
            timeout=10,
        )
    except Exception as e:
        logger.debug("Hook %s failed: %s", event, e)


# ── Hook: on_session_start ─────────────────────────────

def on_session_start(session_id: str, model: str, platform: str, **kwargs) -> None:
    global _session_id
    _session_id = session_id

    # Evict stale guidance trackers to prevent unbounded growth
    if len(_session_guidance_injected) > 1000:
        _session_guidance_injected.clear()

    # Forward to SessionDB via CLI hook
    _fire_hook("sessionstart", {
        "sessionId": session_id,
        "model": model,
        "platform": platform,
        "ts": datetime.now().isoformat(),
    })


# ── Hook: pre_tool_call (PROACTIVE — block before execution) ───

def pre_tool_call(*, tool_name: str, args: dict, task_id: str,
                  session_id: str, **kwargs) -> Optional[dict]:
    """Block high-output terminal commands; log event for analytics."""
    global _session_id
    _session_id = session_id

    # Only intercept Bash tool
    if tool_name not in ("Bash", "execute_code"):
        return None

    command = (args.get("command") or args.get("code") or "").strip()
    if not command:
        return None

    stripped = command.strip().lower()

    # Allowlist
    if _is_allowed(stripped):
        return None

    # Block: known high-output commands
    if tool_name == "Bash" and BLOCKED_HIGH_OUTPUT.search(stripped):
        saved = _count_bytes(args)
        # Forward blocked event to SessionDB
        _fire_hook("posttooluse", {
            "sessionId": session_id,
            "tool_name": "Bash",
            "tool_input": {"command": command},
            "blocked": True,
            "saved_bytes": saved,
            "original_tool": "Bash",
            "sandbox_path": "",
            "error": "Command blocked — high-output, use ctx_execute instead",
        })
        return {
            "action": "deny",
            "message": (
                "Shell command blocked by context-mode policy.\n"
                "Use ctx_execute(language=\"shell\", code=\"...\") instead.\n"
                "Reason: blocked command detected."
            ),
        }

    # Block: inline HTTP in execute_code
    if tool_name == "execute_code" and INLINE_HTTP.search(command):
        saved = _count_bytes(args)
        _fire_hook("posttooluse", {
            "sessionId": session_id,
            "tool_name": "execute_code",
            "tool_input": {"code": command},
            "blocked": True,
            "saved_bytes": saved,
            "original_tool": "execute_code",
            "error": "Inline HTTP blocked — use ctx_fetch_and_index or ctx_execute with fetch inside",
        })
        return {
            "action": "deny",
            "message": (
                "Inline HTTP fetching is blocked by context-mode policy.\n"
                "Use ctx_fetch_and_index(url) or ctx_execute(language=\"javascript\", code=\"…\") "
                "with fetch() inside instead."
            ),
        }

    return None


# ── Hook: transform_tool_result (REACTIVE — sandbox large outputs) ──

def transform_tool_result(*, tool_name: str, args: dict, result: str,
                          session_id: str, **kwargs) -> Optional[str]:
    """Sandbox large outputs; forward event to SessionDB."""
    global _session_id
    _session_id = session_id

    # Never sandbox context-mode MCP tools
    if tool_name in NEVER_SANDBOX:
        return None

    # Only sandbox Bash/terminal with sufficient output
    if tool_name not in ("Bash", "Read", "execute_code"):
        return None

    # Try to unwrap JSON result
    raw_output = result
    try:
        parsed = json.loads(result)
        if isinstance(parsed, dict):
            raw_output = json.dumps(parsed.get("content", parsed), ensure_ascii=False)
    except (json.JSONDecodeError, TypeError):
        pass

    if isinstance(raw_output, str):
        output_len = len(raw_output.encode("utf-8"))
    else:
        output_len = _count_bytes(raw_output)

    if output_len < SANDBOX_THRESHOLD:
        return None

    # Ensure sandbox directory exists
    SANDBOX_DIR.mkdir(parents=True, exist_ok=True)

    count = len(list(SANDBOX_DIR.iterdir())) + 1
    sandbox_path = str(SANDBOX_DIR / f"output-{count}.txt")

    # Write to sandbox file
    with open(sandbox_path, "w") as f:
        f.write(raw_output if isinstance(raw_output, str) else str(raw_output))

    # Forward to SessionDB
    _fire_hook("posttooluse", {
        "sessionId": session_id,
        "tool_name": tool_name,
        "tool_input": args,
        "result": raw_output[:500] if isinstance(raw_output, str) else str(raw_output)[:500],
        "saved_bytes": output_len,
        "sandbox_path": sandbox_path,
        "truncated": True,
    })

    # Compact summary
    return (
        f"{'─' * 40}\n"
        f"Output sandboxed ({output_len} bytes → {sandbox_path})\n"
        f"Output was ~{output_len / 1024:.0f} KB; file saved to sandbox.\n"
        f"{'─' * 40}\n"
    )


# ── Hook: pre_llm_call (inject instructions once per session) ──

def pre_llm_call(*, session_id: str, user_message: str,
                 **kwargs) -> Optional[str]:
    """Inject context optimization instructions on first turn."""
    if session_id in _session_guidance_injected:
        return None

    _session_guidance_injected.add(session_id)

    # Enforce cap
    if len(_session_guidance_injected) > 1000:
        _session_guidance_injected.clear()
        _session_guidance_injected.add(session_id)

    return GUIDANCE


# ── Plugin registration ────────────────────────────────

def register(ctx) -> None:
    ctx.register_hook("pre_tool_call", pre_tool_call)
    ctx.register_hook("transform_tool_result", transform_tool_result)
    ctx.register_hook("pre_llm_call", pre_llm_call)
    ctx.register_hook("on_session_start", on_session_start)
