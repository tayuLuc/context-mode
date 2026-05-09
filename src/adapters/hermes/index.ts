/**
 * adapters/hermes — Hermes Agent platform adapter.
 *
 * Implements HookAdapter for Hermes Agent's MCP + plugin paradigm.
 *
 * Hermes uses:
 *   - Python plugin hooks via .hermes-plugin (pre_tool_call, post_tool_call)
 *   - CLI hook forwarding for analytics: context-mode hook hermes posttooluse
 *   - Native MCP client to connect to context-mode server
 *
 * Session storage: $HERMES_HOME/context-mode/sessions/ (via HERMES_OPTS)
 * Config: ~/.hermes/config.yaml (HERMES_HOME env var override)
 * Plugin: ~/.hermes/plugins/hermes-context-mode/
 * Routing: AGENTS.md
 */

import { resolve, join } from "node:path";
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
// Mirrors HERMES_OPTS in hooks/session-helpers.mjs

function getHermesHome(): string {
  return process.env.HERMES_HOME ?? resolve(homedir(), ".hermes");
}

function getPluginDir(): string {
  return resolve(getHermesHome(), "plugins", "hermes-context-mode");
}

// ── Adapter implementation ───────────────────────────────

class HermesAdapter extends BaseAdapter implements HookAdapter {
  readonly name = "Hermes Agent";
  readonly paradigm: HookParadigm = "mcp-only";

  readonly capabilities: PlatformCapabilities = {
    canBlockTools: true,       // Python plugin blocks via pre_tool_call
    canModifyOutput: true,     // transform_tool_result sandboxes output
    canModifyArgs: false,
    canLogToolCalls: false,    // Handled by Hermes internally
    canInterceptUserPrompts: false,
  };

  readonly hooks: HookRegistration = {};

  constructor() {
    super([".hermes", "plugins", "hermes-context-mode"]);
  }

  // ── Paths ──────────────────────────────────────────────

  getConfigDir(): string {
    return getPluginDir();
  }

  getInstructionFile(): string {
    return "AGENTS.md";
  }

  // ── Diagnostics ──────────────────────────────────────

  diagnostic(): DiagnosticResult[] {
    const hermesHome = getHermesHome();
    const pluginDir = getPluginDir();

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
        status: "ok",
        message: pluginDir,
      },
      {
        name: "MCP Integration",
        value: "full",
        status: "ok",
        message:
          "context-mode MCP tools available via `hermes mcp add`",
      },
      {
        name: "Hooks",
        value: "Python plugin",
        status: "ok",
        message:
          "pre_tool_call, transform_tool_result via .hermes-plugin/; analytics via CLI hook forwarding",
      },
    ];
  }

  // ── Hook stubs (Hermes handles these via Python plugin + CLI hooks) ──

  async handlePreToolUse(
    _event: PreToolUseEvent,
  ): Promise<PreToolUseResponse> {
    return { action: "allow" };
  }

  async handlePostToolUse(
    _event: PostToolUseEvent,
  ): Promise<PostToolUseResponse> {
    return { action: "allow" };
  }

  async handlePreCompact(
    _event: PreCompactEvent,
  ): Promise<PreCompactResponse> {
    return {};
  }

  async handleSessionStart(
    _event: SessionStartEvent,
  ): Promise<SessionStartResponse> {
    return {};
  }
}

export const adapter = new HermesAdapter();
export default adapter;
