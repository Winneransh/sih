# PyInstaller spec for the workbench backend.
#
# One-file mode, deliberately. A Tauri sidecar is a single executable —
# PyInstaller's default one-folder build puts the Python runtime in an
# adjacent _internal/ directory, and copying only the launcher produces a
# binary that starts and immediately dies. One-file embeds everything and
# unpacks to a temp directory at launch: slower to start, but genuinely
# self-contained, which is what the sidecar contract requires.
#
# The hidden imports below are what static analysis misses — uvicorn resolves
# its protocol implementations by string name, so nothing in the source tree
# references them. Those failures surface at runtime, not build time.

from pathlib import Path
from PyInstaller.utils.hooks import collect_data_files, collect_submodules

ROOT = Path(SPECPATH).parent

hiddenimports = [
    "uvicorn.logging",
    "uvicorn.loops.auto", "uvicorn.loops.asyncio",
    "uvicorn.protocols.http.auto", "uvicorn.protocols.http.h11_impl",
    "uvicorn.protocols.websockets.auto", "uvicorn.protocols.websockets.wsproto_impl",
    "uvicorn.lifespan.on", "uvicorn.lifespan.off",
    "anyio._backends._asyncio",
    *collect_submodules("app"),
]

# These ship templates and data tables that must be collected explicitly.
datas = []
for pkg in ("docx", "pptx", "openpyxl", "pint", "sympy"):
    try:
        datas += collect_data_files(pkg)
    except Exception:
        pass

a = Analysis(
    [str(ROOT / "backend_main.py")],
    pathex=[str(ROOT)],
    binaries=[],
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    runtime_hooks=[],
    excludes=["tkinter", "matplotlib", "IPython", "jupyter",
              "pytest", "notebook", "torch", "tensorflow"],
    noarchive=False,
)

pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name="workbench-backend",
    debug=False,
    strip=False,
    upx=False,
    console=True,          # keep stdout so Tauri can surface startup errors
    onefile=True,
)
