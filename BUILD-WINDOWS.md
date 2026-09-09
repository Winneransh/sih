# Building on Windows

This copy targets Windows only. Cross-platform branches have been removed so
there is nothing to work around and nothing that silently applies to another
operating system.

The result is one installer:

```
Industrial AI Workbench_0.1.0_x64-setup.exe
```

Double-click, install, run. No Python, no Node, no runtime for the user to
install.

---

## Prerequisites

Install these once, in this order.

### 1. Visual Studio Build Tools

Download **Build Tools for Visual Studio** from Microsoft and select
**Desktop development with C++** in the installer. Rust needs the MSVC linker
and will not build without it.

### 2. Rust

From <https://rustup.rs>. Accept the defaults, then close and reopen the
terminal so `PATH` updates.

```powershell
rustc --version
cargo install tauri-cli --version "^2"
```

The Tauri CLI compiles from source and takes several minutes.

### 3. Node

From <https://nodejs.org>. LTS is fine.

```powershell
node --version
npm --version
```

### 4. Python 3.12

From <https://www.python.org/downloads/>, **not** the Microsoft Store build —
that one has path restrictions that break PyInstaller. Tick **Add python.exe
to PATH** during installation.

```powershell
python --version
```

### 5. llama.cpp

There is no package manager step here. Download a release build from the
llama.cpp releases page on GitHub:

- **NVIDIA GPU** — take the CUDA build matching your CUDA version
- **No GPU** — take the CPU build

Extract it somewhere permanent, for example `C:\tools\llama.cpp`, and add that
folder to your `PATH`.

```powershell
llama-server.exe --version
```

The build script copies both the executable and the DLLs sitting beside it, so
keep them together.

### 6. WebView2

Present on Windows 11. On Windows 10, install the **WebView2 Runtime** from
Microsoft if the built application refuses to open a window.

---

## Project setup

```powershell
cd path\to\project
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Generate the application icons from any square PNG:

```powershell
cd src-tauri
cargo tauri icon ..\icon.png
cd ..
```

This writes every size Tauri needs, including the `.ico`. The build will not
bundle without them.

---

## Getting a model

```powershell
.\.venv\Scripts\python.exe hfcli.py browse
.\.venv\Scripts\python.exe hfcli.py pull <repository>
.\.venv\Scripts\python.exe engine.py enrich
```

You need at least one text model. Add a vision model for scanned documents and
drawings, and an embedding model for the knowledge base — retrieval will not
run without one.

Models can also be downloaded from inside the application once it is running,
which is the normal route.

---

## Verify before building

```powershell
.\.venv\Scripts\python.exe smoke.py
```

Checks every layer independently and prints pass or fail for each. Nothing is
mocked. Run this before packaging — a failure here is far easier to diagnose
than the same failure inside a frozen executable.

---

## Development

Two terminals, no freezing, fast reload.

```powershell
# terminal 1
.\run.ps1

# terminal 2
cd web
npm run dev
```

Open <http://127.0.0.1:5173>.

To run the desktop shell against those instead of a browser:

```powershell
.\build\dev.ps1
```

The shell finds no sidecar, falls back to port 8000, and picks up frontend
changes live.

---

## Building the installer

```powershell
.\build\build.ps1
```

Four stages: freeze the backend with PyInstaller, copy the sidecars and the
llama.cpp DLLs, build the frontend, bundle.

Output:

```
src-tauri\target\release\bundle\nsis\Industrial AI Workbench_0.1.0_x64-setup.exe
```

If PowerShell refuses to run the script:

```powershell
Set-ExecutionPolicy -Scope Process -ExecutionPolicy Bypass
```

---

## Where data lives

The installed application sits under Program Files, which is read-only, so
everything written at runtime goes to:

```
%LOCALAPPDATA%\IndustrialAIWorkbench
```

Models, sessions, generated files, the search index, logs and the audit record.
It survives uninstallation — remove it by hand if you want a clean slate.

In development none of this applies; data stays in the project folder.

---

## What breaks, and how to tell

**PyInstaller misses an import.** Uvicorn loads its protocol implementations by
string name, so static analysis never sees them. They are listed explicitly in
`build\backend.spec`. If you add a library that loads plugins dynamically, add
it there — the failure appears at runtime, not build time.

**Data files go missing.** The document and calculation libraries ship
templates and tables that must be collected. Already handled; the same applies
to anything new.

**The backend starts alone but not from the app.** Test the frozen binary
directly:

```powershell
.\build\dist\workbench-backend.exe --port 8199
```

If that works and the installed app does not, the problem is in the shell or
the sidecar copy, not the freeze.

**Models fail to load in the packaged app.** Almost always missing DLLs. Check
what shipped:

```powershell
dir "C:\Program Files\Industrial AI Workbench\lib"
```

If that folder is empty, the build script found no DLLs beside
`llama-server.exe` — they were probably separated when it was extracted.

**Orphaned model processes.** The shell kills them on window close. If one
survives a crash:

```powershell
taskkill /F /IM llama-server.exe
```

**Freeze early.** The freeze is the step most likely to consume a day. Run
`build\build.ps1` once well before you need the installer, rather than the day
you do.

---

## Sandbox isolation

Generated code runs in a container with no network when Docker Desktop is
installed. Without it, execution falls back to a subprocess — weaker isolation,
and the smoke test reports it honestly rather than claiming otherwise.

Install Docker Desktop if the isolation claim matters for your deployment.

---

## Server deployment

None of the above applies for a shared GPU machine. Run the backend with
Uvicorn behind a reverse proxy and serve the built frontend as static files.
Users reach it in a browser and nothing is installed on their machines. The
installer is for demonstrations and single-workstation use.
