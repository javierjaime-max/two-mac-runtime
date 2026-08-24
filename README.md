# two-mac-runtime

Turn a MacBook + Mac Mini pair into a reliable personal automation setup:

- **MacBook = console.** Where you work. The lid opens and closes constantly, so nothing recurring is ever scheduled here — anything that is will silently skip while the laptop sleeps.
- **Mac Mini = runtime.** Always on. Every recurring job (cron), every watchdog, everything that must fire on a schedule lives here.

The kit is two Claude Code skills plus battle-tested starter scripts. The patterns come from a setup that has been running this way for months — every rule in here was earned by a real failure.

## Quick start (10 minutes)

1. Clone this repo on your **MacBook**:

```bash
git clone <REPO_URL> ~/two-mac-runtime
```

2. Install the skills:

```bash
cp -r ~/two-mac-runtime/skills/two-mac-runtime ~/.claude/skills/
cp -r ~/two-mac-runtime/skills/pocket-ingest ~/.claude/skills/
```

3. Open Claude Code on the MacBook and say:

> set up my two-mac runtime

The skill walks you (and Claude) through everything: Tailscale on both Macs, SSH keys, granting Full Disk Access to the right python, first cron job on the Mini, heartbeats, and the watchdog — with an attended first run for every piece.

4. When your Pocket AI arrives, say:

> set up my pocket ingestion

## What's in here

| Path | What it is |
|---|---|
| `skills/two-mac-runtime/` | The setup + operating skill. The four laws, the wiring, the conventions. |
| `skills/pocket-ingest/` | Pocket AI → your second brain, hourly, on the Mini. |
| `scripts/stamp.py` | The healthy-exit stamp primitive. ~25 lines. The foundation everything else checks. |
| `scripts/heartbeat_relay.py` | MacBook-side: publishes job health to the Mini every 15 min while awake. |
| `scripts/verify_jobs.py` | Mini-side watchdog: check artifacts → self-heal once → page only if the repair failed. |
| `scripts/pocket_ingest.py` | Pocket pull → classify → route into your vault. Template with a config block. |
| `scripts/templates/` | launchd plist template + `.env` stub. |

## The four laws (short version)

1. **Console vs runtime.** All recurring work runs on the Mini via cron. The only legitimate MacBook jobs are bound to MacBook-only resources (Apple Mail, a local calendar DB) — and they ship heartbeats so the Mini can judge them fairly (asleep ≠ failure).
2. **Attended first run.** New or changed scheduled code never has its first execution unattended. Deploy → run once on the target machine → verify the *produced artifact* (content + today's date, not exit codes, not log timestamps) → only then trust the schedule.
3. **Artifacts over logs; self-heal before paging.** A log that just recorded a crash looks fresh. Every job writes a stamp only on a healthy code path. The watchdog re-runs a stale producer once and pages you only if the repair also fails. You are the last resort, not the monitor.
4. **The sync layer lies.** iCloud Drive will evict files, serve placeholders, and stall silently. Jobs defer on a degraded view instead of guessing; nothing critical depends on the sync layer being healthy at that moment.

Full details, setup steps, and the reasoning behind each rule are in the skills.
