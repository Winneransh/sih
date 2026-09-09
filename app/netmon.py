"""
Network monitor.

An independent observer that polls the machine's outbound connections,
filters out loopback, and reports what remains. During operation that count
should stay at zero — that is the proof the sovereignty claim rests on, and
it is a live counter rather than a statement.

Model downloads are deliberately excluded from the "violation" count and
reported separately: fetching a model from Hugging Face is provisioning, the
same category as installing any software, and it carries no user data. The
distinction is visible rather than hidden.
"""

from __future__ import annotations

import socket
import threading
import time
from collections import deque

try:
    import psutil
except ImportError:
    psutil = None

from .audit import log_event

LOOPBACK = {"127.0.0.1", "::1", "0.0.0.0", "::", "localhost"}

# Hosts contacted for model provisioning. Counted, shown, but not flagged.
PROVISIONING_HOSTS = {"huggingface.co", "cdn-lfs.huggingface.co",
                      "cas-server.xethub.hf.co", "transfer.xethub.hf.co"}


class NetworkMonitor:
    def __init__(self, poll_interval: float = 2.0, history: int = 200):
        self.poll_interval = poll_interval
        self.external_count = 0
        self.provisioning_count = 0
        self.recent = deque(maxlen=history)
        self.started_at = time.time()
        self._seen: set[tuple] = set()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self._lock = threading.Lock()

    # ------------------------------------------------------------- polling

    def _classify(self, raddr) -> str:
        ip = raddr.ip
        if ip in LOOPBACK or ip.startswith("127."):
            return "local"
        host = self._reverse(ip)
        if any(h in host for h in PROVISIONING_HOSTS):
            return "provisioning"
        return "external"

    @staticmethod
    def _reverse(ip: str) -> str:
        try:
            return socket.gethostbyaddr(ip)[0]
        except Exception:
            return ip

    def poll(self) -> list[dict]:
        """One sweep. Returns any newly observed non-local connections."""
        if psutil is None:
            return []
        new = []
        try:
            conns = psutil.net_connections(kind="inet")
        except Exception:
            return []

        for c in conns:
            if not c.raddr or c.status not in ("ESTABLISHED", "SYN_SENT"):
                continue
            kind = self._classify(c.raddr)
            if kind == "local":
                continue

            key = (c.raddr.ip, c.raddr.port, c.pid)
            if key in self._seen:
                continue
            self._seen.add(key)

            proc = ""
            try:
                if c.pid:
                    proc = psutil.Process(c.pid).name()
            except Exception:
                pass

            rec = {
                "ts": time.time(),
                "remote": f"{c.raddr.ip}:{c.raddr.port}",
                "host": self._reverse(c.raddr.ip),
                "process": proc,
                "pid": c.pid,
                "kind": kind,
            }
            with self._lock:
                self.recent.append(rec)
                if kind == "external":
                    self.external_count += 1
                    log_event("netmon.external", **rec)
                else:
                    self.provisioning_count += 1
            new.append(rec)
        return new

    # ------------------------------------------------------------- control

    def _loop(self):
        while not self._stop.is_set():
            self.poll()
            self._stop.wait(self.poll_interval)

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        log_event("netmon.start")

    def stop(self):
        self._stop.set()
        log_event("netmon.stop", external=self.external_count)

    def snapshot(self) -> dict:
        with self._lock:
            return {
                "available": psutil is not None,
                "external_calls": self.external_count,
                "provisioning_calls": self.provisioning_count,
                "sovereign": self.external_count == 0,
                "uptime_s": round(time.time() - self.started_at, 1),
                "recent": list(self.recent)[-25:],
            }

    def reset(self):
        with self._lock:
            self.external_count = 0
            self.provisioning_count = 0
            self.recent.clear()
            self._seen.clear()
            self.started_at = time.time()


monitor = NetworkMonitor()
