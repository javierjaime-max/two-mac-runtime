#!/usr/bin/env python3
"""
verify_jobs.py — Mini (runtime) side watchdog. Law 3 in code:
artifacts over logs; self-heal before paging; the user is the last resort.

For every job in JOBS: read its healthy-exit stamp (never trust a log's
mtime). Stale or unhealthy → re-run the producer ONCE with a bounded wait →
re-check. Only what STILL fails after the repair becomes an exception.

Console (MacBook) jobs are judged via the heartbeat the laptop ships:
asleep is not failure — a job is only flagged if the heartbeat says the
laptop has been awake long enough for it to have run.

Report by exception: clean → one line appended to a local log, no email.
Exceptions → one plain-language email under 15 lines, with a cooldown so a
persistent failure pages once, not every run.

Schedule: Mini cron, once each morning, e.g.
  0 7 * * * PATH=/opt/homebrew/bin:/usr/bin:/bin /opt/homebrew/bin/python3 ~/scripts/verify_jobs.py >> /tmp/verify-jobs.log 2>&1

Hard-won rules already encoded here:
- A MISSING stamp is "unknown", never an age computed since epoch 0 — a cold
  start is not a two-million-hour outage.
- The cooldown state file is written AFTER paging, so the cooldown survives.
- Never fabricate a check you could not run — an unreadable heartbeat is
  itself an exception, reported honestly.
"""
import json
import os
import smtplib
import subprocess
from datetime import datetime, timedelta, timezone
from email.mime.text import MIMEText

# ── CONFIG — fill these in ──────────────────────────────────────────────────
_ST = os.path.expanduser("~/.runtime/stamps")

# Mini-resident jobs: stamp path, max healthy age, and the exact command that
# re-runs the producer (None = no self-heal, just report).
JOBS = {
    # "pocket-ingest": {
    #     "stamp": f"{_ST}/pocket-ingest.stamp",
    #     "max_age_min": 120,
    #     "heal": ["/opt/homebrew/bin/python3",
    #              os.path.expanduser("~/scripts/pocket_ingest.py")],
    # },
}

# Console (MacBook) jobs, judged from the heartbeat it ships:
CONSOLE_HEARTBEAT = os.path.expanduser("~/.runtime/console-heartbeat.json")
CONSOLE_JOB_MAX_AGE_MIN = {
    # "mail sweep (hourly)": 180,
}
CONSOLE_ABSENT_H = 20        # heartbeat older than this = flag the console itself
CONSOLE_MIN_AWAKE_MIN = 120  # don't judge a console job unless awake this long

ENV_FILE = os.path.expanduser("~/.runtime/.env")   # GMAIL_ADDRESS / GMAIL_APP_PASSWORD
STATE = os.path.expanduser("~/.runtime/verify_state.json")
REPORT_LOG = os.path.expanduser("~/.runtime/verify_log.md")
HEAL_TIMEOUT_S = 600
PAGE_COOLDOWN_H = 6
# ────────────────────────────────────────────────────────────────────────────


def read_stamp(path):
    """Returns (age, status, detail). A missing stamp is (None, ...) — unknown,
    never an age since epoch 0."""
    try:
        st = json.load(open(path))
        age = datetime.now(timezone.utc) - datetime.fromisoformat(st["when"])
        return age, st.get("status", "ok"), str(st.get("detail", ""))[:300]
    except Exception:
        return None, "missing", "no readable stamp"


def check_job(name, cfg):
    """Returns None if healthy, else a one-line problem description."""
    age, status, detail = read_stamp(cfg["stamp"])
    if age is not None and age <= timedelta(minutes=cfg["max_age_min"]) and status == "ok":
        return None
    if age is None:
        problem = "never stamped (unknown — has it had its attended first run?)"
    elif status != "ok":
        problem = f"ran {int(age.total_seconds()//60)}m ago but status={status}: {detail}"
    else:
        problem = f"stale — last healthy exit {int(age.total_seconds()//3600)}h ago"

    heal = cfg.get("heal")
    if not heal:
        return f"{name}: {problem} (no self-heal configured)"
    try:
        r = subprocess.run(heal, capture_output=True, timeout=HEAL_TIMEOUT_S,
                           env={**os.environ,
                                "PATH": "/opt/homebrew/bin:/usr/bin:/bin"})
        heal_note = f"re-ran producer (exit {r.returncode})"
    except Exception as e:
        heal_note = f"re-run attempt failed to launch: {e.__class__.__name__}"
    age2, status2, detail2 = read_stamp(cfg["stamp"])
    if age2 is not None and age2 <= timedelta(minutes=15) and status2 == "ok":
        return None  # self-healed — not an exception, noted in the clean line
    return f"{name}: {problem}; {heal_note}; still unhealthy after repair"


def check_console():
    """Judge laptop jobs fairly: asleep is not failure."""
    problems = []
    try:
        hb = json.load(open(CONSOLE_HEARTBEAT))
        now = datetime.now(timezone.utc)
        hb_age = now - datetime.fromisoformat(hb["written_at"])
        awake = now - datetime.fromisoformat(hb["awake_since"])
    except FileNotFoundError:
        return ["console: no heartbeat ever received (unknown — is the relay set up?)"]
    except Exception as e:
        return [f"console: heartbeat unreadable ({e.__class__.__name__}) — itself an exception"]

    if hb_age > timedelta(hours=CONSOLE_ABSENT_H):
        return [f"console: no heartbeat in {int(hb_age.total_seconds()//3600)}h "
                f"(laptop lost, relay dead, or genuinely away)"]
    if hb_age > timedelta(minutes=45):
        return []  # laptop is asleep right now — not a failure, nothing to judge

    for job, det in hb.get("unhealthy", {}).items():
        problems.append(f"console job '{job}': runs but unhealthy — "
                        f"{det.get('status')}: {det.get('detail', '')}")
    if awake >= timedelta(minutes=CONSOLE_MIN_AWAKE_MIN):
        for job, max_age in CONSOLE_JOB_MAX_AGE_MIN.items():
            beat = hb.get("jobs", {}).get(job)
            if beat is None:
                problems.append(f"console job '{job}': no artifact yet (unknown)")
            elif (datetime.now(timezone.utc) - datetime.fromisoformat(beat)
                  ) > timedelta(minutes=max_age):
                problems.append(f"console job '{job}': stale despite laptop awake "
                                f"{int(awake.total_seconds()//3600)}h")
    return problems


def load_env():
    env = {}
    try:
        for line in open(ENV_FILE):
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    except OSError:
        pass
    return env


def page(exceptions):
    """One plain-language email, under 15 lines. Returns True if sent."""
    env = load_env()
    addr, pw = env.get("GMAIL_ADDRESS"), env.get("GMAIL_APP_PASSWORD")
    if not (addr and pw):
        print("PAGE SKIPPED: GMAIL_ADDRESS / GMAIL_APP_PASSWORD not set in ~/.runtime/.env")
        return False
    body = "Morning verification found problems that survived one repair attempt:\n\n"
    body += "\n".join(f"- {e}" for e in exceptions[:10])
    body += "\n\nEverything not listed here checked out clean."
    msg = MIMEText(body)
    msg["Subject"] = f"RUNTIME EXCEPTION — {datetime.now().date()}"
    msg["From"] = msg["To"] = addr
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=30) as s:
        s.login(addr, pw)
        s.send_message(msg)
    return True


def main():
    exceptions = [p for name, cfg in JOBS.items() if (p := check_job(name, cfg))]
    exceptions += check_console()

    stamp_line = f"- {datetime.now():%Y-%m-%d %H:%M} verification: "
    if not exceptions:
        stamp_line += "ALL CLEAN"
        print("ALL CLEAN")
    else:
        stamp_line += f"{len(exceptions)} exception(s): " + "; ".join(exceptions)
        print("\n".join(exceptions))
        state = {}
        try:
            state = json.load(open(STATE))
        except Exception:
            pass
        last = state.get("last_paged")
        due = (last is None
               or datetime.now(timezone.utc) - datetime.fromisoformat(last)
               > timedelta(hours=PAGE_COOLDOWN_H))
        if due:
            try:
                if page(exceptions):
                    state["last_paged"] = datetime.now(timezone.utc).isoformat()
            except Exception as e:
                print(f"PAGE FAILED: {e}")
        else:
            print(f"page suppressed (cooldown, last paged {last})")
        # Written AFTER paging — writing only before would discard the cooldown
        # stamp every run and turn a six-hour cooldown into an email per run.
        with open(STATE, "w") as f:
            json.dump(state, f)

    try:
        with open(REPORT_LOG, "a") as f:
            f.write(stamp_line + "\n")
    except OSError:
        pass


if __name__ == "__main__":
    main()
    from stamp import stamp
    stamp("verify-jobs")
