/**
 * adapters/hermes — Hermes Agent platform adapter.
 *
 * Implements HookAdapter for Hermes Agent's MCP + plugin paradigm.
 *
 * Hermes hook specifics:
 *   - Python plugin hooks via .hermes-plugin (pre_tool_call, transform_tool_result, etc.)
 *   - MCP: full support via `hermes mcp add context-mode`
 *   - Config: ~/.hermes/ (HERMES_HOME env var)
 *   - Session dir: ~/.hermes/plugins/hermes-context-mode/sessions/
 *   - Routing file: AGENTS.md
 *
 * Sources:
 *   - Hermes Agent: https://github.com/nousresearch/hermes-agent
 *   - HERMES_HOME env var (defaults to ~/.hermes/)
 */

import { readFileSync, writeFileSync, mkdirSync } from "node:fs";
import { resolve, dirname } from "node:path";
import { homedir, platform } from "node:os";

import { BaseAdapter } from "../base.js";

import type {
  HookAdapter,
  HookParadigm,
  PlatformCapabilities,
  DiagnosticResult,
  PreToolUseEvent,
  PostToolUseEvent,
  PreCompactEvent,
  SessionStartEvent,
  PreToolUseResponse,
  PostToolUseResponse,
  PreCompactResponse,
  SessionStartResponse,
  HookRegistration,
} from "../types.js";

// ── Hermes Home Resolution ───────────────────────────────

function getHermesHome(): string {
  return (
    process.env.HERMES_HOME ??
    resolve(homedir(), ".hermes")
  );
}

function getHermesPluginDir(): string {
  return resolve(getHermesHome(), "plugins", "hermes-context-mode");
}

function getHermesSessionDir(): string {
  return resolve(getHermesPluginDir(), "sessions");
}

// ── Adapter implementation ───────────────────────────────

export class HermesAdapter extends BaseAdapter implements HookAdapter {
  constructor() {
    super([".hermes", "plugins", "hermes-context-mode"]);
  }

  readonly name = "Hermes Agent";
  readonly paradigm: HookParadigm = "mcp-only";

  readonly capabilities: PlatformCapabilities = {
    canBlockTools: true,       // pre_tool_call blocks curl/wget
    canModifyOutput: true,     // transform_tool_result sandboxes output
    canModifyArgs: false,
    canInjectSessionContext: true, // pre_llm_call injects routing rules
    hasSessionStart: true,     // on_session_start
    hasPreCompact: false,
    hasPostToolUse: true,      // transform_tool_result
    hasPreToolUse: true,       // pre_tool_call
  };

  readonly hooks: HookRegistration = {};

  /** Hermes uses HERMES_HOME, not ~/.hermes, so we override getSessionDir. */
  getSessionDir(): string {
    const dir = getHermesSessionDir();
    mkdirSync(dir, { recursive: true });
    return dir;
  }

  getInstructionFile(): string {
    return "AGENTS.md";
  }

  getConfigDir(): string {
    return getHermesPluginDir();
  }

  /** Get the stats file path for the current session. */
  getStatsFilePath(sessionId?: string): string {
    const id = sessionId ?? process.env.HERMES_SESSION_ID ?? `pid-${process.pid}`;
    return resolve(this.getSessionDir(), `stats-${id}.json`);
  }

  // ── Diagnostics ──────────────────────────────────────

  diagnostic(): DiagnosticResult[] {
    const hermesHome = getHermesHome();
    const pluginDir = getHermesPluginDir();

    return [
      {
        name: "Hermes Home",
        value: hermesHome,
        status: "ok",
        message: hermesHome,
      },
      {
        name: "Plugin Directory",
        value: pluginDir,
        status: pluginDir.length > 10 ? "ok" : "warning",
        message: pluginDir,
      },
      {
        name: "Session Directory",
        value: this.getSessionDir(),
        status: "ok",
        message: this.getSessionDir(),
      },
      {
        name: "MCP Integration",
        value: "full",
        status: "ok",
        message: `context-mode MCP tools available via \`hermes mcp add\``,
      },
      {
        name: "Plugin Hooks",
        value: "5 hooks",
        status: "ok",
        message: "pre_tool_call, transform_tool_result, pre_llm_call, on_session_start, on_session_end",
      },
    ];
  }

  // ── Stub hooks (Hermes handles these via Python plugin) ──

  async handlePreToolUse(event: PreToolUseEvent): Promise<PreToolUseResponse> {
    return { action: "allow" };
  }

  async handlePostToolUse(event: PostToolUseEvent): Promise<PostToolUseResponse> {
    return { action: "allow" };
  }

  async handlePreCompact(event: PreCompactEvent): Promise<PreCompactResponse> {
    return {};
  }

  async handleSessionStart(event: SessionStartEvent): Promise<SessionStartResponse> {
    return {};
  }
}
