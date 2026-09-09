# Industrial AI Workbench — Windows

Self-hosted agentic workbench on local open-weight models. Nothing leaves the
machine.

This copy targets Windows only. See `BUILD-WINDOWS.md` for prerequisites,
setup and packaging.

## Quick start

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
.\.venv\Scripts\python.exe smoke.py
.\run.ps1
```

Then in a second terminal:

```powershell
cd web
npm install
npm run dev
```

Open <http://127.0.0.1:5173>.

## Layout

```
app\
  config.py       paths, limits, agent-to-capability map
  manifest.py     installed models and their capabilities
  supervisor.py   spawns model servers, ports, health, reaping
  router.py       model name to port
  llm.py          chat / vision / embed / rerank — the only model client
  planner.py      decides agents, models, cardinality, parallelism, fan-in
  executor.py     walks the plan; no decisions
  composer.py     writes or tightens the reply
  textcheck.py    degenerate-output detection
  store.py        conversations, runs, audit (SQLite)
  audit.py        append-only event log
  netmon.py       outbound connection counter
  server.py       HTTP API
  rag\            index and deterministic document resolver
  agents\         general, vision, rag, code, calc, docx, pptx, xlsx
  tools\          intake, files, sandbox, document generation

web\              React interface
src-tauri\        desktop shell
build\            packaging
```

## Pipeline

```
A model is added      acquire -> register -> serve
A document is added   chunk -> embed -> store

A request arrives     intake -> resolve -> plan -> route -> execute
                      -> compose -> deliver
```

Ingestion, resolution, routing and execution are code. Planning, the agents
and composition use a model. Nothing else does.

## Key endpoints

```
GET  /api/models/browse?q=          model repository
GET  /api/models/info/{repo}        card and quantizations with fit badges
POST /api/models/pull               download and register
POST /api/models/load/{repo}        start a model server
DELETE /api/models/eject/{repo}     stop and delete

POST /api/chat/stream               plan and step events over SSE
POST /api/files/upload              indexed on arrival if it has a text layer
GET  /api/files/deliverables/{sid}

POST /api/kb/ingest_upload
GET  /api/kb/search?q=

GET  /api/system/health
GET  /api/system/network            outbound connection count
GET  /api/system/audit
GET  /api/system/agents
```

## Scope limits worth stating

- **Engineering drawings**: tag numbers, line numbers and title block. Symbol
  classification and topology extraction are not attempted.
- **Handwriting**: works on clear hands; degrades on poor scans.
- **Sandbox**: real isolation requires Docker Desktop. Without it, execution
  falls back to a subprocess and reports that honestly.
- **Vector store**: a JSON index, fine at demonstration scale. Swap it behind
  the search function when the corpus grows.
