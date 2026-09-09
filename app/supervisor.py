"""
Process supervisor.

Owns the llama-server processes: spawn, port assignment, health check,
shutdown, orphan reaping. This is the only component that knows anything
about hardware, binaries or command-line flags. Everything above it sees
a port and an OpenAI-compatible endpoint.

Swapping in MLX or vLLM later means adding another build_cmd/spawn pair
behind the same three functions — start, health, stop.
"""

import json
import os
import signal
import socket
import subprocess
import time
from pathlib import Path

import httpx

from . import manifest
from .config import (
    LLAMA_SERVER, RUNTIME_PATH, LOGS_DIR, DEFAULT_CTX, HEALTH_TIMEOUT,
)


def _load_runtime() -> dict:
    if not RUNTIME_PATH.exists():
        return {}
    return json.loads(RUNTIME_PATH.read_text())


def _save_runtime(rt: dict) -> None:
    RUNTIME_PATH.write_text(json.dumps(rt, indent=2))


def _pid_running(pid: int) -> bool:
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


def _free_port(start: int = 8080) -> int:
    for p in range(start, start + 300):
        with socket.socket() as s:
            if s.connect_ex(("127.0.0.1", p)) != 0:
                return p
    raise RuntimeError("no free port available")


def reap() -> dict:
    """Drop runtime entries whose process has died. Keeps state honest."""
    rt = _load_runtime()
    live = {r: i for r, i in rt.items() if _pid_running(i["pid"])}
    if len(live) != len(rt):
        _save_runtime(live)
    return live


def is_alive(port: int) -> bool:
    try:
        return httpx.get(f"http://127.0.0.1:{port}/health", timeout=1.5).status_code == 200
    except httpx.HTTPError:
        return False


def build_cmd(entry: dict, port: int, ctx: int) -> list[str]:
    d = Path(entry["path"])
    cmd = [
        LLAMA_SERVER,
        "--model", str(d / entry["weight_file"]),
        "--port", str(port),
        "--host", "127.0.0.1",
        "--ctx-size", str(ctx),
    ]
    if entry.get("mmproj_file"):
        cmd += ["--mmproj", str(d / entry["mmproj_file"])]

    # These endpoints do not exist unless the flag is passed at startup.
    caps = entry.get("capabilities") or []
    if "embedding" in caps:
        cmd += ["--embedding", "--pooling", "mean"]
    if "reranker" in caps:
        cmd += ["--reranking"]
    return cmd


def _wait_healthy(port: int, proc, timeout: int = HEALTH_TIMEOUT) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if proc.poll() is not None:
            return False
        if is_alive(port):
            return True
        time.sleep(1.0)
    return False


def start(model_name: str, ctx: int = DEFAULT_CTX) -> dict:
    """Start a model if it isn't already up. Returns its runtime record."""
    entry = manifest.resolve(model_name)
    repo_id = entry["repo_id"]

    rt = reap()
    if repo_id in rt and is_alive(rt[repo_id]["port"]):
        return rt[repo_id]

    used = {i["port"] for i in rt.values()}
    port = _free_port()
    while port in used:
        port = _free_port(port + 1)

    log = LOGS_DIR / f"{repo_id.replace('/', '__').lower()}.log"

    # Detached so the model survives a backend restart — reloading weights on
    # every code change would make development impractical. The consequence is
    # that these processes must be reaped deliberately, which the desktop
    # shell does on close.
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.DETACHED_PROCESS

    with open(log, "ab") as fh:
        proc = subprocess.Popen(
            build_cmd(entry, port, ctx),
            stdout=fh, stderr=fh,
            creationflags=flags,
        )

    if not _wait_healthy(port, proc):
        try:
            proc.terminate()
        except Exception:
            pass
        raise RuntimeError(f"{repo_id} failed to start — see {log}")

    rec = {
        "model": repo_id,
        "port": port,
        "pid": proc.pid,
        "capabilities": entry.get("capabilities", []),
        "started_at": time.time(),
    }
    rt[repo_id] = rec
    _save_runtime(rt)
    return rec


def stop(model_name: str | None = None) -> list[str]:
    """Stop one model, or all of them."""
    rt = _load_runtime()
    targets = list(rt) if model_name is None else [
        r for r in rt if model_name.lower() in r.lower()
    ]
    stopped = []
    for repo in targets:
        try:
            os.kill(rt[repo]["pid"], signal.SIGTERM)
        except OSError:
            pass
        rt.pop(repo, None)
        stopped.append(repo)
    _save_runtime(rt)
    return stopped


def status() -> list[dict]:
    rt = reap()
    out = []
    for repo, i in rt.items():
        out.append({**i, "healthy": is_alive(i["port"])})
    return out


def ensure(model_names: list[str], ctx: int = DEFAULT_CTX) -> dict:
    """Start several models, returning {model: record} for those that came up."""
    started = {}
    for name in model_names:
        try:
            started[name] = start(name, ctx)
        except Exception as e:
            started[name] = {"error": str(e)}
    return started
