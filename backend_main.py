#!/usr/bin/env python3
"""
Entry point for the frozen backend.

Tauri spawns this as a sidecar. It differs from `run.sh` in two ways that
matter once the app is packaged:

  - Data lives in a per-user directory, not next to the executable. A frozen
    app sits under Program Files, where it cannot write.
  - The port can be passed in, so the shell can pick a free one rather than
    failing when 8000 is taken.
"""

import argparse
import os
import sys
from pathlib import Path


def data_dir() -> Path:
    """
    Per-user writable location for models, sessions, logs and the index.

    The installed application sits under Program Files, which is read-only, so
    anything written at runtime goes here instead.
    """
    base = Path(os.environ.get("LOCALAPPDATA", Path.home() / "AppData/Local"))
    d = base / "IndustrialAIWorkbench"
    d.mkdir(parents=True, exist_ok=True)
    return d


def _prepend(var: str, path: str) -> None:
    current = os.environ.get(var, "")
    parts = [p for p in current.split(os.pathsep) if p]
    if path not in parts:
        os.environ[var] = os.pathsep.join([path, *parts])


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8000)
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--data-dir", default=None)
    args = ap.parse_args()

    # config.py reads this before creating any directories, so it must be set
    # before the app package is imported.
    os.environ["WORKBENCH_DATA_DIR"] = args.data_dir or str(data_dir())

    # llama-server ships beside the frozen executable, and its shared
    # libraries ship as app resources. Without pointing the loader at them
    # the binary starts and immediately dies on a missing DLL.
    if getattr(sys, "frozen", False):
        bundled = Path(sys.executable).parent
        for name in ("llama-server", "llama-server.exe"):
            if (bundled / name).exists():
                os.environ.setdefault("LLAMA_SERVER", str(bundled / name))
                break

        # llama-server.exe is dynamically linked and its DLLs ship as app
        # resources. Without putting that directory on PATH the process starts
        # and immediately exits on a missing library.
        for candidate in (bundled / "lib", bundled.parent / "lib",
                          bundled / "resources" / "lib"):
            if candidate.is_dir():
                lib = str(candidate)
                _prepend("PATH", lib)
                # Python 3.8+ ignores PATH for extension DLLs; be explicit.
                try:
                    os.add_dll_directory(lib)
                except (AttributeError, OSError):
                    pass
                print(f"library path: {lib}", flush=True)
                break

    import uvicorn
    print(f"data dir: {os.environ['WORKBENCH_DATA_DIR']}", flush=True)
    print(f"listening on http://{args.host}:{args.port}", flush=True)
    uvicorn.run("app.server:app", host=args.host, port=args.port,
                log_level="info", access_log=False)


if __name__ == "__main__":
    main()
