# Start the backend for development. Frontend runs separately:
#     cd web ; npm run dev
$ErrorActionPreference = "Stop"
Set-Location $PSScriptRoot
& .\.venv\Scripts\python.exe -m uvicorn app.server:app --host 127.0.0.1 --port 8000
