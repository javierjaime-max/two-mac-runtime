---
name: pocket-ingest
description: Wire a Pocket AI voice recorder into the user's second brain — set up the Pocket MCP server, then run an hourly ingest loop on the Mac Mini that pulls new recordings, classifies them, and files them as markdown notes into the user's vault. Use when the user says "set up my pocket ingestion", "my pocket arrived", or asks to get Pocket recordings into their notes/vault.
---

# pocket-ingest

Pocket (heypocketai) is a wearable voice recorder. This skill turns it into an automatic capture channel: everything the user records routes itself into their second brain hourly, with no action needed from them.

**Prerequisite:** the two-mac-runtime setup (this repo's other skill). The ingest loop is a Mini cron job and follows all four laws — especially the attended first run.

## Step 1 — MCP server setup (when the Pocket arrives)

1. The user needs their Pocket API key: Pocket app → Settings → Developer / API. Have them put it in `~/.runtime/.env` as `POCKET_API_KEY=...` themselves — never paste it through chat.
2. For interactive use in Claude Code, add the MCP server:

```bash
claude mcp add --transport http pocket https://public.heypocketai.com/mcp --header "Authorization: Bearer $POCKET_API_KEY"
```

3. Verify in an interactive session: ask Claude to list recent Pocket conversations. Note the exact tool names the server exposes (e.g. `search_pocket_conversations`, `get_pocket_conversation`) — the ingest script calls these over HTTP and MUST use the server's real tool names. Confirm them during the attended first run; do not trust the names written here.

## Step 2 — ask the user two things

1. **Vault path** — where their second brain lives (e.g. an Obsidian vault in iCloud: `~/Library/Mobile Documents/com~apple~CloudDocs/<Vault>`). The skill assumes it exists; the only structure it needs is an inbox folder.
2. **Categories** — 4–8 buckets that match their life (e.g. `fleetos`, `personal`, `family`, `ideas`, `tasks`). These become subfolders of `inbox/pocket/`.

## Step 3 — deploy the ingest loop (on the Mini)

`scripts/pocket_ingest.py` is the template. Fill in its CONFIG block (vault path, categories, API key location), copy it to the Mini at `~/scripts/pocket_ingest.py`, then follow the loop's design:

- **Pull** new recordings/transcripts from the Pocket MCP server over HTTP using `POCKET_API_KEY` (headless — no browser OAuth).
- **Idempotent ledger:** `~/.runtime/pocket_routed.json`, keyed by recording ID. Only new recordings are processed; re-runs are always safe.
- **Classify** each transcript into one of the user's categories using headless Claude:
  - `claude -p` with an ISOLATED config: `CLAUDE_CONFIG_DIR=~/.runtime/claude-config` and `--strict-mcp-config`. Without this, headless claude tries to load the machine's full MCP/plugin setup and hangs.
  - Requires a subscription token on the Mini: run `claude setup-token` there once (attended).
- **Route:** write a markdown note with frontmatter (date, source: pocket, category, recording ID) into `<vault>/inbox/pocket/<category>/`.
- **Quarantine on doubt:** if classification is uncertain, the note goes to `<vault>/inbox/pocket/review/` — never auto-filed into the wrong bucket. Misfiling is worse than a review queue.
- **Stamp:** ends with `stamp("pocket-ingest", ...)`, with `status="degraded"` if the Pocket API was reachable but returned errors.

## Step 4 — attended first run (Law 2 — do not skip)

1. Have the user make a short test recording on the Pocket.
2. Run the script manually on the Mini: `ssh <user>@<mini>.local 'PATH=/opt/homebrew/bin:/usr/bin:/bin /opt/homebrew/bin/python3 ~/scripts/pocket_ingest.py'`
3. Verify the ARTIFACT: open the created note in the vault — right folder, correct frontmatter, transcript present, today's date.
4. Verify the ledger recorded the ID, and a second manual run processes nothing (idempotency proven).
5. Only then add the cron line — hourly, offset from the top of the hour to avoid colliding with other jobs (e.g. `7 * * * *`).
6. Add a `pocket-ingest` row to `verify_jobs.py`'s JOBS table (max age ~2h) in the same session.

## Failure modes to expect

- **Pocket API unreachable** → stamp `degraded`, exit cleanly, try again next hour. Do not page on one miss.
- **Vault path is an evicted iCloud placeholder** → defer (skip the write, stamp `degraded` with detail), never write into a half-synced vault.
- **`claude -p` hangs** → almost always the missing isolated `CLAUDE_CONFIG_DIR` / `--strict-mcp-config`, or an expired token (`claude setup-token` again).
