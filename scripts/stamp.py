#!/usr/bin/env python3
"""
stamp — healthy-exit stamps for every scheduled job (Law 3: artifacts over logs).

A log's mtime lies: a job that crashes writes its traceback to the log and looks
alive. A stamp is written ONLY on the healthy code path — the canonical pattern:

    if __name__ == "__main__":
        main()                      # an exception here skips the stamp
        from stamp import stamp
        stamp("job-name")

A fresh stamp is not automatically a HEALTHY stamp: a job that ran on time but
accomplished nothing must say so —

    stamp("job-name", status="degraded", detail="0 items processed")

heartbeat_relay (MacBook) and verify_jobs (Mini) prefer stamps over logs
wherever a stamp exists. Stamps live in ~/.runtime/stamps/<job>.stamp.
"""
import json
import os
from datetime import datetime

STAMPS = os.path.expanduser("~/.runtime/stamps")


def stamp(job, status="ok", **extra):
    try:
        os.makedirs(STAMPS, exist_ok=True)
        json.dump({"when": datetime.now().astimezone().isoformat(),
                   "status": status, **extra},
                  open(os.path.join(STAMPS, f"{job}.stamp"), "w"))
    except Exception:
        pass  # a stamp failure must never break the job itself


def stamp_path(job):
    return os.path.join(STAMPS, f"{job}.stamp")
