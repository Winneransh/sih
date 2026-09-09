"""
File tools. Pure functions, no model.

Everything is jailed to a session directory. An agent cannot read or write
outside it — this is the trust boundary, enforced in code rather than by
convention.
"""

from __future__ import annotations

import shutil
from pathlib import Path

from ..audit import log_event
from ..config import SESSIONS_DIR


def session_dir(session_id: str) -> Path:
    d = SESSIONS_DIR / session_id
    (d / "inputs").mkdir(parents=True, exist_ok=True)
    (d / "work").mkdir(parents=True, exist_ok=True)
    (d / "outputs").mkdir(parents=True, exist_ok=True)
    return d


def _jail(session_id: str, path: str | Path) -> Path:
    """Resolve a path and refuse anything that escapes the session directory."""
    root = session_dir(session_id).resolve()
    p = Path(path)
    full = (root / p).resolve() if not p.is_absolute() else p.resolve()
    if not str(full).startswith(str(root)):
        raise PermissionError(f"path escapes session workspace: {path}")
    return full


def read_file(session_id: str, path: str, max_chars: int = 100_000) -> str:
    p = _jail(session_id, path)
    text = p.read_text(encoding="utf-8", errors="replace")
    log_event("tool.read_file", session_id=session_id, path=str(p), chars=len(text))
    return text[:max_chars]


def write_file(session_id: str, path: str, content: str) -> str:
    p = _jail(session_id, path)
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(content, encoding="utf-8")
    log_event("tool.write_file", session_id=session_id, path=str(p), chars=len(content))
    return str(p)


def list_files(session_id: str, subdir: str = "") -> list[dict]:
    root = _jail(session_id, subdir) if subdir else session_dir(session_id)
    out = []
    for p in sorted(root.rglob("*")):
        if p.is_file():
            out.append({
                "path": str(p.relative_to(session_dir(session_id))),
                "size": p.stat().st_size,
            })
    return out


def stage_upload(session_id: str, src: str | Path, filename: str | None = None) -> Path:
    """Copy a user upload into the session's inputs directory."""
    src = Path(src)
    dst = session_dir(session_id) / "inputs" / (filename or src.name)
    shutil.copy2(src, dst)
    log_event("tool.stage_upload", session_id=session_id,
              src=str(src), dst=str(dst), size=dst.stat().st_size)
    return dst


def outputs_dir(session_id: str) -> Path:
    return session_dir(session_id) / "outputs"


def work_dir(session_id: str) -> Path:
    return session_dir(session_id) / "work"
