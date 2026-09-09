# Windows build.
#
# Freezes the backend, collects the sidecars, builds the frontend, and bundles
# an installer. Run from the project root in PowerShell:
#
#     .\build\build.ps1
#
# Produces src-tauri\target\release\bundle\nsis\*-setup.exe

$ErrorActionPreference = "Stop"

Set-Location (Split-Path -Parent $PSScriptRoot)
$Root = Get-Location

# Tauri names sidecars <name>-<target-triple> and refuses to bundle one whose
# triple does not match the host.
$Triple = (& rustc -vV | Select-String '^host:').ToString().Split(' ')[1]
Write-Host "target triple: $Triple"

$Py = ".\.venv\Scripts\python.exe"
if (-not (Test-Path $Py)) {
    throw "No virtual environment at .venv. Run: python -m venv .venv"
}

$BinDir = "src-tauri\binaries"
if (Test-Path $BinDir) { Remove-Item -Recurse -Force $BinDir }
New-Item -ItemType Directory -Force -Path $BinDir | Out-Null

# ------------------------------------------------------------- 1. backend
Write-Host ""
Write-Host "==> freezing the backend"
& $Py -m pip install --quiet pyinstaller
& $Py -m PyInstaller --noconfirm --clean `
    --distpath build\dist --workpath build\work `
    build\backend.spec
if ($LASTEXITCODE -ne 0) { throw "PyInstaller failed" }

# One-file mode puts the executable directly in dist\, not in a subfolder.
$Frozen = "build\dist\workbench-backend.exe"
if (-not (Test-Path $Frozen)) { throw "freeze produced no $Frozen" }

# ------------------------------------------------------------ 2. sidecars
Write-Host ""
Write-Host "==> collecting sidecars"

Copy-Item $Frozen "$BinDir\workbench-backend-$Triple.exe" -Force

$Llama = (Get-Command llama-server.exe -ErrorAction SilentlyContinue)
if (-not $Llama) {
    throw @"
llama-server.exe is not on PATH.

Download a release build from the llama.cpp releases page, extract it, and add
that folder to PATH. Choose the CUDA build if this machine has an NVIDIA GPU,
otherwise the CPU build.
"@
}
$LlamaPath = $Llama.Source
$LlamaDir = Split-Path -Parent $LlamaPath
Write-Host "    llama-server: $LlamaPath"

Copy-Item $LlamaPath "$BinDir\llama-server-$Triple.exe" -Force

# llama-server.exe is dynamically linked. On Windows its DLLs sit beside the
# executable rather than in a lib\ sibling, and copying only the exe produces
# a binary that cannot start. They ship as resources and the backend points
# the loader at them.
$LibDst = "src-tauri\resources\lib"
if (Test-Path $LibDst) { Remove-Item -Recurse -Force $LibDst }
New-Item -ItemType Directory -Force -Path $LibDst | Out-Null

$Dlls = Get-ChildItem -Path $LlamaDir -Filter *.dll -ErrorAction SilentlyContinue
foreach ($d in $Dlls) { Copy-Item $d.FullName $LibDst -Force }

Write-Host "    bundled $($Dlls.Count) llama.cpp libraries"
if ($Dlls.Count -eq 0) {
    Write-Warning "No DLLs found beside llama-server.exe."
    Write-Warning "The packaged app may fail to start models."
}

# ------------------------------------------------------------ 3. frontend
Write-Host ""
Write-Host "==> building the frontend"
Push-Location web
if (Test-Path package-lock.json) { npm ci --silent } else { npm install --silent }
npm run build
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "frontend build failed" }
Pop-Location

# ----------------------------------------------------------- 4. installer
Write-Host ""
Write-Host "==> bundling"
Push-Location src-tauri
cargo tauri build
if ($LASTEXITCODE -ne 0) { Pop-Location; throw "tauri build failed" }
Pop-Location

Write-Host ""
Write-Host "done. Installer is in src-tauri\target\release\bundle\nsis\"
Get-ChildItem "src-tauri\target\release\bundle\nsis\*.exe" -ErrorAction SilentlyContinue |
    ForEach-Object { Write-Host "    $($_.Name)  $([math]::Round($_.Length/1MB,1)) MB" }
