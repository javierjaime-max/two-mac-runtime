---
name: two-mac-runtime
description: Set up and operate a MacBook (console) + Mac Mini (always-on runtime) pair — Tailscale/SSH wiring, cron on the Mini, launchd only for laptop-bound jobs, healthy-exit stamps, heartbeats, and a self-healing watchdog. Use when the user asks to set up their two-mac runtime, add a scheduled job, wire a heartbeat, or debug why a scheduled job silently isn't running.
---

# two-mac-runtime

You are setting up or operating a two-Mac personal automation pair:

- **MacBook = console.** Interactive work only. The lid closes constantly; anything scheduled here silently skips while asleep.
- **Mac Mini = runtime.** Always on. ALL recurring work runs here via cron.

The scripts referenced below live in the repo this skill shipped in (`~/two-mac-runtime/scripts/` if cloned per the README). Each has a CONFIG block at the top — fill it in for this user's hostnames, username, and paths during setup.

## The four laws (violating these is a defect)

1. **Console vs runtime.** Every recurring job is a Mini cron entry. The ONLY legitimate MacBook-resident jobs are bound to MacBook-only resources (Apple Mail's local store, a local Calendar DB, an app that only runs there). Those jobs ship heartbeats and the Mini judges them fairly: asleep ≠ failure; stale beyond ~20 waking hours = flag.
2. **Attended first run.** New or changed scheduled code NEVER first-executes unattended: deploy → run once on the target machine under real conditions → verify the PRODUCED ARTIFACT (content and today's date — not exit codes, not log mtimes) → only then trust the schedule.
3. **Artifacts over logs; self-heal before paging.** A log that just recorded a crash has a fresh mtime. Every job writes a stamp (`stamp.py`) only on its healthy code path. The watchdog (`verify_jobs.py`) checks stamps, re-runs a stale producer ONCE with a bounded wait, and pages the user ONLY if the repair also fails. A stamp must describe work done, not code reached — pass `status="degraded"` / `detail=...` when the job ran but accomplished nothing (a job that runs on time and does nothing is the worst failure mode, because everything looks green).
4. **The sync layer lies (iCloud edition).** iCloud Drive's real path is `~/Library/Mobile Documents/com~apple~CloudDocs/`. It evicts files to placeholders, stalls in either direction without error, and lags after mass edits. Defenses:
   - On the Mini: System Settings → Apple ID → iCloud Drive → turn OFF "Optimize Mac Storage", or scheduled jobs will read evicted placeholder files.
   - `brctl download <path>` forces materialization; `brctl monitor` watches sync activity.
   - Jobs that read a shared folder must defer on a degraded view (missing/placeholder files) rather than resolve against a partial picture.
   - Don't schedule vault-wide Mini jobs immediately after a large MacBook-side edit burst — give sync time to settle.
   - Anything machine-to-machine that matters goes over SSH first; the iCloud copy is a convenience mirror, never the delivery path.

## Phase 0 — one-time setup (walk the user through, verify each step)

1. **Mini: enable Remote Login.** System Settings → General → Sharing → Remote Login ON. Note the Mini's hostname (`hostname` → e.g. `Nicks-Mac-mini.local`) and username.
2. **Tailscale on both Macs.** Install from https://tailscale.com/download, sign both into the SAME tailnet (same account). Record the Mini's tailnet IP (`tailscale ip -4` on the Mini, a `100.x.x.x` address). Tailscale makes the pair work from anywhere — coffee shop, travel — not just the home LAN.
3. **Key-based SSH from MacBook → Mini.** `ssh-keygen -t ed25519` if no key exists, then `ssh-copy-id <user>@<mini>.local`. Verify: `ssh -o BatchMode=yes <user>@<mini>.local hostname` succeeds with no password prompt. All automated SSH uses `-o ConnectTimeout=4 -o BatchMode=yes` so nothing ever hangs waiting for a prompt.
4. **Homebrew python on both machines** (`brew install python`). Scheduled jobs use the ABSOLUTE path `/opt/homebrew/bin/python3` — that is the interpreter you grant Full Disk Access to (System Settings → Privacy & Security → Full Disk Access). The system `/usr/bin/python3` cannot be granted access to `~/Library/Mobile Documents` and will fail silently on iCloud paths.
5. **Runtime directory on both machines:** `mkdir -p ~/.runtime/stamps`. Stamps, heartbeats, and watchdog state live here — local disk, never inside iCloud.
6. **Credentials stub:** copy `scripts/templates/config.example.env` to `~/.runtime/.env`, then have the USER fill in the Gmail app password themselves (create at https://myaccount.google.com/apppasswords). Never ask them to paste secrets into a chat or a command line — they edit the file directly.

## Connectivity pattern (use everywhere)

```python
TS_HOST  = "100.x.x.x"            # Mini's tailnet IP — first, works from anywhere
LAN_HOST = "Minis-hostname.local"  # LAN fallback when the tailnet is down
SSH_OPTS = ["-o", "ConnectTimeout=4", "-o", "BatchMode=yes"]
for host in (TS_HOST, LAN_HOST):
    ...  # try each, first success wins
```

Interactive form: `ssh <user>@<host> '<cmd>'`.

## Job conventions

- **Mini jobs = cron** (`crontab -e` on the Mini), with an explicit `PATH=/opt/homebrew/bin:/usr/bin:/bin` line at the top of the crontab — cron's default PATH won't find Homebrew tools.
- **MacBook jobs = launchd** (only when laptop-bound): use `scripts/templates/com.USER.JOB.plist`, install to `~/Library/LaunchAgents/`, `launchctl load` it. `RunAtLoad true` so it also fires at wake/login — laptop jobs must be idempotent for exactly this reason.
- **Every job ends with a stamp:**

```python
if __name__ == "__main__":
    main()                     # an exception here skips the stamp
    from stamp import stamp
    stamp("job-name")          # or stamp("job-name", status="degraded", detail="0 items processed")
```

- Logs go to `/tmp/<job>.log` (stdout+stderr both). Logs are for reading after a failure; stamps are for machines to judge health.
- Retired launchd jobs move to `~/Library/LaunchAgents/_retired/` (after `launchctl unload`), never deleted — you will want the config back.
- **Every new loop joins the watchdog at creation time** — add its row to the `JOBS` table in `verify_jobs.py` in the same session that creates it. A loop nothing watches does not exist.
- **Report by exception.** Clean run = silence (one line appended to a dated log at most). Failure = one plain-language email under 15 lines: which artifact, stamped when, what repair was attempted, what the error was. No shorthand codes. Never fabricate a check you could not run — "ssh unreachable" is itself an exception, reported honestly.

## The three scripts

1. **`stamp.py`** → copy to `~/scripts/` on BOTH machines. The primitive everything else reads. A stamp write failure must never break the job itself.
2. **`heartbeat_relay.py`** → MacBook, launchd every 15 min (`StartInterval 900`). Publishes each MacBook job's stamp time + health to the Mini over scp (Tailscale-first, LAN fallback). It derives `awake_since` locally — the Mini can't tell "job is 14h stale" from "laptop woke 4 minutes ago", but the laptop can. Delivery is SSH FIRST, cloud mirror second and best-effort: a heartbeat whose delivery depends on the sync layer cannot report that the sync layer is stuck.
3. **`verify_jobs.py`** → Mini, cron once each morning (e.g. `0 7 * * *`). Reads every stamp, judges MacBook jobs against the heartbeat's `awake_since`, self-heals stale Mini producers once, pages via SMTP only for what still fails. Two hard-won rules are already encoded: a MISSING stamp is "unknown", never age-since-epoch (a cold start is not a two-million-hour outage); and the paging-cooldown state file is written AFTER paging, so the cooldown actually survives.

## Setting up the first job (do this WITH the user, Law 2)

1. Write the job script on the Mini with its stamp call.
2. Run it manually over ssh: `ssh <user>@<mini>.local 'PATH=/opt/homebrew/bin:/usr/bin:/bin /opt/homebrew/bin/python3 ~/scripts/<job>.py'`
3. Open the produced artifact. Check its CONTENT and that it's stamped today.
4. `cat ~/.runtime/stamps/<job>.stamp` — verify status "ok".
5. Only now add the crontab line, and add the job's row to `verify_jobs.py`'s table.
6. Next morning, confirm the scheduled run produced a fresh artifact on its own.

## Debugging "my job didn't run"

Check in this order:
1. Which machine is it scheduled on? If the MacBook — was the lid open at fire time? (This is the #1 cause. Move it to the Mini unless it's laptop-bound.)
2. `cat ~/.runtime/stamps/<job>.stamp` — when did it last exit healthy, and what status?
3. `/tmp/<job>.log` — read the actual error, remembering the log's freshness proves nothing.
4. Is the path it reads inside iCloud? Check for placeholder eviction (`brctl download`), and whether the interpreter is the Full-Disk-Access one.
5. cron: does the crontab have the PATH line? launchd: `launchctl list | grep <label>` — is it loaded, and what was the last exit code?
