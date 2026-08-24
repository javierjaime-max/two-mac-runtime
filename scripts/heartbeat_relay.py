#!/usr/bin/env python3
"""
heartbeat_relay.py — MacBook (console) side of the watchdog.

Every 15 minutes while the MacBook is awake, publish each MacBook-resident
job's last-healthy-exit time and status to the Mini. The Mini's verify_jobs.py
reads this and judges every laptop loop honestly: green, stale, or dead —
and, crucially, "asleep" instead of "failed".

Two delivery lanes, because a cloud-sync folder between machines can stall
for hours without error:
  1. DIRECT: scp straight to the Mini (Tailscale-first, LAN fallback). Primary.
  2. Cloud mirror (iCloud). Best-effort, for a human browsing — never the
     delivery path. A heartbeat whose delivery depends on the sync layer
     cannot report that the sync layer is stuck, which is most of what it
     exists to tell us.

Schedule with launchd: StartInterval 900, RunAtLoad true (see templates/).
"""

import json
import os
import subprocess
from datetime import datetime, timezone

# ── CONFIG — fill these in for your machines ────────────────────────────────
TS_HOST = "100.x.x.x"                  # Mini's tailnet IP: `tailscale ip -4`
LAN_HOST = "Your-Mac-mini.local"       # Mini's LAN hostname: `hostname`
RUNTIME_USER = "youruser"              # your username on the Mini

# (healthy-exit stamp, log fallback) per MacBook-resident job. The stamp is
# written only when the job's main() returns cleanly; the log is used only
# until the job's first healthy run creates its stamp. A log's mtime LIES —
# a crash writes a traceback and looks alive.
_ST = os.path.expanduser("~/.runtime/stamps")
JOBS = {
    # "mail sweep (hourly)": (f"{_ST}/mail-sweep.stamp", "/tmp/mail-sweep.log"),
    # "calendar sync (15 min)": (f"{_ST}/cal-sync.stamp", "/tmp/cal-sync.log"),
}

# Optional best-effort cloud mirror (set to None to disable):
CLOUD_MIRROR = os.path.expanduser(
    "~/Library/Mobile Documents/com~apple~CloudDocs/runtime/heartbeats/console.json")
# ────────────────────────────────────────────────────────────────────────────

LOCAL = os.path.expanduser("~/.runtime/console-heartbeat.json")
SSH_OPTS = ["-o", "ConnectTimeout=4", "-o", "BatchMode=yes"]


def ship_direct(local_path):
    """scp the heartbeat straight to the Mini; returns the host that worked."""
    for host in (TS_HOST, LAN_HOST):
        try:
            r = subprocess.run(
                ["scp", "-q"] + SSH_OPTS
                + [local_path,
                   f"{RUNTIME_USER}@{host}:.runtime/console-heartbeat.json"],
                capture_output=True, timeout=15)
            if r.returncode == 0:
                return host
        except Exception:
            continue
    return None


def main():
    beats = {}
    statuses = {}
    for name, (stamp_p, log_p) in JOBS.items():
        path = stamp_p if os.path.exists(stamp_p) else log_p
        try:
            mtime = os.stat(path).st_mtime
            beats[name] = datetime.fromtimestamp(mtime, timezone.utc).isoformat()
        except OSError:
            beats[name] = None  # unknown — never fake an age
        # A FRESH stamp is not a HEALTHY stamp. Relay the status the stamp
        # actually carries so the Mini can page on a job that runs on time
        # and does nothing.
        if os.path.exists(stamp_p):
            try:
                st = json.load(open(stamp_p))
                if st.get("status", "ok") != "ok":
                    statuses[name] = {"status": st.get("status"),
                                      "detail": str(st.get("detail", ""))[:300]}
            except Exception:
                statuses[name] = {"status": "unreadable",
                                  "detail": "stamp is not valid JSON"}

    # How long has this laptop been continuously awake?
    #
    # "The heartbeat is fresh" only says the machine is up NOW. The Mini needs
    # to know whether a laptop-side job has HAD TIME to run: a job 14h stale
    # is broken if the lid has been open for hours, and nothing at all if the
    # laptop woke four minutes ago. Deriving it on the Mini silently assumes
    # the machine never slept in between — exactly the wrong assumption for a
    # laptop. This side can just answer it: beats land every 15 minutes while
    # awake, so a gap materially larger than that means a fresh wake.
    now = datetime.now(timezone.utc)
    awake_since = now
    try:
        prev = json.load(open(LOCAL))
        gap = (now - datetime.fromisoformat(prev["written_at"])).total_seconds()
        if gap < 45 * 60:                      # 3 missed beats of slack
            awake_since = datetime.fromisoformat(
                prev.get("awake_since") or prev["written_at"])
    except Exception:
        pass                                   # no history: treat now as the wake

    payload = {
        "written_at": now.isoformat(),
        "awake_since": awake_since.isoformat(),
        "jobs": beats,
        "unhealthy": statuses,  # jobs whose stamp says it ran but did not work
    }

    # SSH FIRST, cloud mirror second — and never let the cloud write stop the
    # ship. This once ran backwards: the cloud write threw on a locked file,
    # the exception killed main() before ship_direct() ran, and the runtime
    # concluded the laptop had not woken in 52 hours. It was awake the whole time.
    os.makedirs(os.path.dirname(LOCAL), exist_ok=True)
    with open(LOCAL, "w") as f:
        json.dump(payload, f, indent=1)
    shipped = ship_direct(LOCAL)

    mirrored = False
    if CLOUD_MIRROR:
        try:
            os.makedirs(os.path.dirname(CLOUD_MIRROR), exist_ok=True)
            with open(CLOUD_MIRROR, "w") as f:
                json.dump(payload, f, indent=1)
            mirrored = True
        except OSError as e:
            print(f"cloud mirror skipped ({e.__class__.__name__}: {e.errno})")
    print(f"heartbeat shipped: {shipped or 'FAILED — no route to the Mini'}"
          f" | cloud mirror: {'ok' if mirrored else 'skipped'}")


if __name__ == "__main__":
    main()
