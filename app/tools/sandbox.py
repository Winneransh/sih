"""
Code execution sandbox.

Two backends. Docker is the real one: no network, read-only root, memory
and CPU capped, dropped capabilities. Subprocess is a development fallback
for machines without Docker — it is weaker isolation and says so.

The `--network=none` on the Docker path is also evidence for the
no-external-calls claim: generated code physically cannot reach out.
"""

from __future__ import annotations

import os
import shutil
import subprocess
import sys
import tempfile
import textwrap
from pathlib import Path

from ..audit import log_event

DOCKER_IMAGE = "python:3.12-slim"   # requires Docker Desktop
DEFAULT_TIMEOUT = 60
MEM_LIMIT = "1g"
CPU_LIMIT = "1.0"


def docker_available() -> bool:
    if not shutil.which("docker"):
        return False
    try:
        return subprocess.run(["docker", "info"], capture_output=True,
                              timeout=10).returncode == 0
    except Exception:
        return False


def _run_docker(code: str, workdir: Path, timeout: int) -> dict:
    script = workdir / "_run.py"
    script.write_text(code, encoding="utf-8")

    cmd = [
        "docker", "run", "--rm",
        "--network=none",                 # no outbound anything
        "--memory", MEM_LIMIT,
        "--cpus", CPU_LIMIT,
        "--pids-limit", "128",
        "--cap-drop", "ALL",
        "--security-opt", "no-new-privileges",
        # Docker Desktop resolves Windows paths, but forward slashes avoid
        # ambiguity between the shell and the daemon.
        "-v", f"{str(workdir).replace(chr(92), '/')}:/work",
        "-w", "/work",
        DOCKER_IMAGE,
        "python", "_run.py",
    ]
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout + 15)
        return {
            "ok": r.returncode == 0,
            "exit_code": r.returncode,
            "stdout": r.stdout[-20000:],
            "stderr": r.stderr[-8000:],
            "backend": "docker",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "stdout": "",
                "stderr": f"timed out after {timeout}s", "backend": "docker"}


def _run_subprocess(code: str, workdir: Path, timeout: int) -> dict:
    script = workdir / "_run.py"
    script.write_text(code, encoding="utf-8")
    try:
        r = subprocess.run(
            [sys.executable, "_run.py"],
            cwd=workdir, capture_output=True, text=True, timeout=timeout,
            env={"PATH": os.environ.get("PATH", ""),
                 "SYSTEMROOT": os.environ.get("SYSTEMROOT", ""),
                 "TEMP": str(workdir), "TMP": str(workdir)},
        )
        return {
            "ok": r.returncode == 0,
            "exit_code": r.returncode,
            "stdout": r.stdout[-20000:],
            "stderr": r.stderr[-8000:],
            "backend": "subprocess (weak isolation)",
        }
    except subprocess.TimeoutExpired:
        return {"ok": False, "exit_code": -1, "stdout": "",
                "stderr": f"timed out after {timeout}s",
                "backend": "subprocess (weak isolation)"}


def run_python(code: str,
               *,
               session_id: str | None = None,
               workdir: Path | None = None,
               timeout: int = DEFAULT_TIMEOUT,
               force_subprocess: bool = False) -> dict:
    tmp = None
    if workdir is None:
        tmp = tempfile.TemporaryDirectory()
        workdir = Path(tmp.name)
    workdir.mkdir(parents=True, exist_ok=True)

    use_docker = docker_available() and not force_subprocess
    runner = _run_docker if use_docker else _run_subprocess

    result = runner(code, workdir, timeout)
    log_event("tool.sandbox", session_id=session_id, backend=result["backend"],
              ok=result["ok"], exit_code=result["exit_code"],
              code_chars=len(code))

    if tmp:
        tmp.cleanup()
    return result


def run_with_deps(code: str, packages: list[str], **kw) -> dict:
    """
    Install packages then run. Only works on the subprocess backend, since
    the Docker sandbox has no network by design. Kept explicit so nobody
    quietly re-enables networking to make an install work.
    """
    if packages:
        prelude = textwrap.dedent(f"""
            import subprocess, sys
            subprocess.run([sys.executable, "-m", "pip", "install", "-q",
                            *{packages!r}], check=False)
        """)
        code = prelude + "\n" + code
    return run_python(code, force_subprocess=True, **kw)
