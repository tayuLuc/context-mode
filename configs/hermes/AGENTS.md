# context-mode — MANDATORY routing rules

context-mode MCP tools available. Rules protect context window from flooding. One unrouted command dumps 56 KB into context.

## Think in Code — MANDATORY

Analyze/count/filter/compare/search/parse/transform data: **write code** via `ctx_execute(language, code)`, `console.log()` only the answer. Do NOT read raw data into context. PROGRAM the analysis, not COMPUTE it. Use Python or JavaScript — Node.js built-ins only (`fs`, `path`, etc. for JS). `try/catch`, handle edge cases. One script replaces ten tool calls.

## BLOCKED — do NOT attempt

### curl / wget — BLOCKED
Shell `curl`/`wget` intercepted and blocked. Do NOT retry.
Use: `ctx_execute(language: "javascript", code: "const r = await fetch(url)")` or `ctx_fetch_and_index(url, source)`.

### Inline HTTP — BLOCKED
`fetch('http`, `requests.get(`, `requests.post(`, `http.get(`, `urllib.request` — intercepted. Do NOT retry.
Use: `ctx_execute(language, code)` — only stdout enters context.

### Direct web fetching — BLOCKED
Use: `ctx_fetch_and_index(url, source)` then `ctx_search(queries)`.

## REDIRECTED — use sandbox

### Shell (>20 lines output)
Shell ONLY for: `git`, `mkdir`, `rm`, `mv`, `cd`, `ls`, `npm install`, `pip install`, `brew`, `hermes`.
Otherwise: `ctx_batch_execute(commands, queries)` or `ctx_execute(language: "shell", code: "...")`.

### File reading (for analysis)
Reading to **edit** → use `read_file` or `patch`. Reading to **analyze/explore/summarize** → `ctx_execute(language, code)` with `fs.readFileSync()` / `open()`.

## UTILITY COMMANDS

When the user says one of these, call the MCP tool:

| User says | Action |
|-----------|--------|
| `ctx stats` | Call `stats` MCP tool, display full output verbatim |
| `ctx doctor` | Call `doctor` MCP tool, run returned shell command, display as checklist |
| `ctx upgrade` | Call `upgrade` MCP tool, run returned shell command, display as checklist |
| `ctx purge` | Call `purge` MCP tool with confirm: true. Warns before wiping knowledge base. |

After `/clear` or `/compact`: knowledge base and session stats preserved. Use `ctx purge` to start fresh.
