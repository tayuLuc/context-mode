#!/usr/bin/env node
import "../suppress-stderr.mjs";
import "../ensure-deps.mjs";
/**
 * Hermes preCompact hook — build resume snapshot.
 */

import { readStdin, parseStdin, getSessionId, getSessionDBPath, getInputProjectDir, HERMES_OPTS } from "../session-helpers.mjs";
import { dirname } from "node:path";
import { fileURLToPath } from "node:url";
import { createSessionLoaders } from "../session-loaders.mjs";

const HOOK_DIR = dirname(fileURLToPath(import.meta.url));
const { loadSessionDB } = createSessionLoaders(HOOK_DIR);
const OPTS = HERMES_OPTS;

try {
  const raw = await readStdin();
  const input = parseStdin(raw);
  const projectDir = getInputProjectDir(input, OPTS);
  const { SessionDB } = await loadSessionDB();

  const dbPath = getSessionDBPath(OPTS);
  const db = new SessionDB({ dbPath });
  const sessionId = getSessionId(input, OPTS);

  // Build resume snapshot from session events
  const allEvents = db.getEvents(sessionId);
  const stats = db.getSessionStats(sessionId);
  const { buildResumeSnapshot } = await import("../../session/snapshot.js");
  const snapshot = buildResumeSnapshot(allEvents, {
    projectDir,
    platform: "hermes",
    toolCount: stats?.total_calls ?? 0,
  });

  db.upsertResume(sessionId, snapshot, allEvents.length);
  db.incrementCompactCount(sessionId);
  db.close();
} catch (err) {
  console.error('[hermes/precompact] Error:', err);
}
