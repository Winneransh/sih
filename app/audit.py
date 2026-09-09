"""
Append-only audit trail.

An observer, never a participant. Agents cannot write to it directly and
cannot switch it off. Every model call, agent run, tool call, file write
and KB query lands here with a timestamp.

Format is JSON Lines so it can be tailed, grepped and streamed to the UI
without parsing the whole file.
"""

import json
import threading
import time
from datetime import datetime, timezone

from .config import AUDIT_PATH

_lock = threading.Lock()


def log_event(event: str, **fields) -> dict:
    rec = {
        "ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"),
        "event": event,
        **fields,
    }
    line = json.dumps(rec, default=str)
    with _lock:
        with open(AUDIT_PATH, "a", encoding="utf-8") as fh:
            fh.write(line + "\n")
    return rec


def tail(n: int = 200, session_id: str | None = None) -> list[dict]:
    if not AUDIT_PATH.exists():
        return []
    lines = AUDIT_PATH.read_text(encoding="utf-8", errors="replace").splitlines()
    out = []
    for ln in reversed(lines):
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if session_id and rec.get("session_id") != session_id:
            continue
        out.append(rec)
        if len(out) >= n:
            break
    return list(reversed(out))


class Timer:
    """Context manager that logs start/end with elapsed time."""

    def __init__(self, event: str, **fields):
        self.event = event
        self.fields = fields
        self.t0 = 0.0

    def __enter__(self):
        self.t0 = time.time()
        log_event(f"{self.event}.start", **self.fields)
        return self

    def __exit__(self, exc_type, exc, tb):
        elapsed = round(time.time() - self.t0, 2)
        if exc:
            log_event(f"{self.event}.error", elapsed_s=elapsed,
                      error=str(exc), **self.fields)
        else:
            log_event(f"{self.event}.end", elapsed_s=elapsed, **self.fields)
        return False
