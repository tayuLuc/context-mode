#!/usr/bin/env node
import "../suppress-stderr.mjs";
/**
 * Hermes preToolUse hook for context-mode.
 */

import { dirname, resolve } from "node:path";
import { fileURLToPath } from "node:url";
import { readStdin, parseStdin, getInputProjectDir, getSessionId, HERMES_OPTS } from "../session-helpers.mjs";
import { routePreToolUse, initSecurity } from "../core/routing.mjs";
import { formatDecision } from "../core/formatters.mjs";

const __hookDir = dirname(fileURLToPath(import.meta.url));
await initSecurity(resolve(__hookDir, "..", "..", "build"));

const raw = await readStdin();
const input = parseStdin(raw);
const tool = input.tool_name ?? "";
const toolInput = input.tool_input ?? {};
const projectDir = getInputProjectDir(input, HERMES_OPTS);

const decision = routePreToolUse(tool, toolInput, projectDir, "hermes", getSessionId(input, HERMES_OPTS));
const response = formatDecision("hermes", decision);
process.stdout.write(JSON.stringify(response ?? { agent_message: "" }) + "\n");
