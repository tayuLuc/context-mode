#!/usr/bin/env node
import "../suppress-stderr.mjs";
import "../ensure-deps.mjs";
/**
 * Hermes postToolUse hook — session event capture.
 */

import { readStdin, parseStdin, getSessionId, getSessionDBPath, getInputProjectDir, HERMES_OPTS } from "../session-helpers.mjs";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createSessionLoaders, attributeAndInsertEvents } from "../session-loaders.mjs";

const HOOK_DIR = dirname(fileURLToPath(import.meta.url));
const { loadSessionDB, loadExtract, loadProjectAttribution } = createSessionLoaders(HOOK_DIR);
const OPTS = HERMES_OPTS;

function normalizeToolName(toolName) {
  // Hermes uses PascalCase tool names (Bash, Read, Write, Edit, etc.)
  return toolName;
}

try {
  const raw = await readStdin();
  const input = parseStdin(raw);
  const projectDir = getInputProjectDir(input, OPTS);

  const { extractEvents } = await loadExtract();
  const { resolveProjectAttributions } = await loadProjectAttribution();
  const { SessionDB } = await loadSessionDB();

  const dbPath = getSessionDBPath(OPTS);
  const db = new SessionDB({ dbPath });
  const sessionId = getSessionId(input, OPTS);

  db.ensureSession(sessionId, projectDir);

  const normalizedInput = {
    tool_name: normalizeToolName(input.tool_name ?? ""),
    tool_input: input.tool_input ?? {},
    tool_response: typeof input.result === "string"
      ? input.result
      : JSON.stringify(input.result ?? input.error ?? ""),
    tool_output: input.error
      ? { isError: true }
      : undefined,
  };

  const events = extractEvents(normalizedInput);

  // If blocked — tag events with context savings from the data field
  if (input.blocked) {
    for (const ev of events) {
      ev.data = JSON.stringify({
        blocked: true,
        savedBytes: input.saved_bytes ?? 0,
        sandboxPath: input.sandbox_path ?? "",
        originalTool: input.original_tool ?? "",
      });
    }
  }

  attributeAndInsertEvents(db, sessionId, events, input, projectDir, "PostToolUse", resolveProjectAttributions);

  db.close();
} catch {
  // Silent fallback — postToolUse must never block the session
}

process.stdout.write(JSON.stringify({ additional_context: "" }) + "\n");
