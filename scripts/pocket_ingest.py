#!/usr/bin/env python3
"""
pocket_ingest.py — Pocket AI recordings → your second brain. Runs hourly on
the Mini (cron). Template: fill in the CONFIG block, then do an ATTENDED
first run (Law 2) before scheduling.

Pipeline:
  1. Pull recent conversations/transcripts from the Pocket MCP server over
     HTTP (API-key auth, no browser).
  2. Skip anything already in the ledger (~/.runtime/pocket_routed.json,
     keyed by conversation ID) — re-runs are always safe.
  3. Classify each transcript into one of your categories with headless
     Claude (`claude -p`, isolated config so it can't hang loading MCP setup).
  4. Write a markdown note with frontmatter into <vault>/inbox/pocket/<category>/.
     Uncertain classification → inbox/pocket/review/ — never auto-filed wrong.
  5. Stamp healthy exit; stamp "degraded" if Pocket was unreachable.

IMPORTANT before first run:
- Verify the MCP tool name below against what the Pocket server actually
  exposes (list tools in an interactive Claude session first). The name
  used here is the best-known guess, not a guarantee.
- Run `claude setup-token` once on the Mini (subscription auth for -p).

Cron (offset from the top of the hour to avoid job pileups):
  7 * * * * PATH=/opt/homebrew/bin:/usr/bin:/bin /opt/homebrew/bin/python3 ~/scripts/pocket_ingest.py >> /tmp/pocket-ingest.log 2>&1
"""
import json
import os
import re
import subprocess
import urllib.request
from datetime import datetime

# ── CONFIG — fill these in ──────────────────────────────────────────────────
VAULT = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/YOUR-VAULT")
CATEGORIES = ["personal", "work", "family", "ideas", "tasks"]

POCKET_MCP_URL = "https://public.heypocketai.com/mcp"
POCKET_LIST_TOOL = "search_pocket_conversations"   # VERIFY on attended first run
ENV_FILE = os.path.expanduser("~/.runtime/.env")   # holds POCKET_API_KEY=...

CLAUDE_BIN = "/opt/homebrew/bin/claude"            # `which claude` on the Mini
CLAUDE_CONFIG = os.path.expanduser("~/.runtime/claude-config")
# ────────────────────────────────────────────────────────────────────────────

LEDGER = os.path.expanduser("~/.runtime/pocket_routed.json")
MAX_PER_RUN = 20


def api_key():
    for line in open(ENV_FILE):
        if line.strip().startswith("POCKET_API_KEY="):
            return line.strip().split("=", 1)[1]
    raise RuntimeError("POCKET_API_KEY not found in ~/.runtime/.env")


def mcp_call(tool, arguments, key):
    """Minimal MCP-over-HTTP tools/call. Adjust if the server's contract differs."""
    req = urllib.request.Request(
        POCKET_MCP_URL,
        data=json.dumps({"jsonrpc": "2.0", "id": 1, "method": "tools/call",
                         "params": {"name": tool, "arguments": arguments}}).encode(),
        headers={"Content-Type": "application/json",
                 "Accept": "application/json, text/event-stream",
                 "Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as r:
        raw = r.read().decode()
    # Streamable-HTTP servers may answer as an SSE frame; take the data line.
    for line in raw.splitlines():
        if line.startswith("data:"):
            raw = line[5:].strip()
            break
    return json.loads(raw)


def fetch_recent(key):
    """Return a list of {id, title, transcript, created_at}. Shape depends on
    the Pocket server — reshape here after inspecting a real response on the
    attended first run."""
    resp = mcp_call(POCKET_LIST_TOOL, {"query": "", "limit": MAX_PER_RUN}, key)
    content = resp.get("result", {}).get("content", [])
    text = " ".join(c.get("text", "") for c in content if c.get("type") == "text")
    try:
        items = json.loads(text)
        return items if isinstance(items, list) else items.get("conversations", [])
    except Exception:
        raise RuntimeError(f"unexpected Pocket response shape: {text[:200]}")


def classify(transcript):
    """Headless Claude, isolated config (Law: without --strict-mcp-config and
    its own CLAUDE_CONFIG_DIR, claude -p hangs loading the machine's MCP setup).
    Returns a category from CATEGORIES, or 'review' when uncertain."""
    prompt = (
        "Classify this voice-capture transcript into exactly one category from: "
        f"{', '.join(CATEGORIES)}. If you are not confident, answer: review. "
        "Answer with the single category word only.\n\n---\n" + transcript[:6000])
    try:
        r = subprocess.run(
            [CLAUDE_BIN, "-p", prompt, "--strict-mcp-config"],
            capture_output=True, text=True, timeout=120,
            env={**os.environ, "CLAUDE_CONFIG_DIR": CLAUDE_CONFIG,
                 "PATH": "/opt/homebrew/bin:/usr/bin:/bin"})
        answer = r.stdout.strip().lower().split()[-1] if r.stdout.strip() else ""
        return answer if answer in CATEGORIES else "review"
    except Exception:
        return "review"   # quarantine on doubt — misfiling is worse than a queue


def write_note(item, category):
    folder = os.path.join(VAULT, "inbox", "pocket", category)
    os.makedirs(folder, exist_ok=True)
    title = re.sub(r"[^\w\s-]", "", item.get("title") or "capture").strip()[:60] or "capture"
    stamp_str = datetime.now().strftime("%Y-%m-%d-%H%M")
    path = os.path.join(folder, f"{stamp_str} {title}.md")
    front = (f"---\nsource: pocket\ncaptured: {item.get('created_at', '')}\n"
             f"category: {category}\npocket_id: {item.get('id', '')}\n---\n\n")
    with open(path, "w") as f:
        f.write(front + (item.get("transcript") or "").strip() + "\n")
    return path


def main():
    from stamp import stamp
    # Law 4: never write into a half-synced vault. If the vault folder is
    # missing (unmounted, evicted), defer to the next hour.
    if not os.path.isdir(VAULT):
        print(f"vault not reachable at {VAULT} — deferring")
        stamp("pocket-ingest", status="degraded", detail="vault not reachable")
        return

    ledger = {}
    try:
        ledger = json.load(open(LEDGER))
    except Exception:
        pass

    try:
        items = fetch_recent(api_key())
    except Exception as e:
        print(f"pocket unreachable: {e}")
        stamp("pocket-ingest", status="degraded", detail=f"pocket unreachable: {e}")
        return

    routed = 0
    for item in items:
        cid = str(item.get("id") or "")
        if not cid or cid in ledger:
            continue
        transcript = (item.get("transcript") or "").strip()
        category = classify(transcript) if transcript else "review"
        path = write_note(item, category)
        ledger[cid] = {"routed_at": datetime.now().astimezone().isoformat(),
                       "category": category, "path": path}
        routed += 1
        print(f"routed {cid} -> {category}")

    with open(LEDGER, "w") as f:
        json.dump(ledger, f, indent=1)
    print(f"done: {routed} new, {len(items) - routed} already routed/skipped")
    stamp("pocket-ingest", detail=f"{routed} new")


if __name__ == "__main__":
    main()
