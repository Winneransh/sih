"""
FastAPI backend.

Endpoint groups:

  /api/models    browse Hugging Face, download, enrich, load, unload, eject
  /api/chat      the main entry point — plan, execute, stream events
  /api/sessions  conversation persistence
  /api/kb        ingest and search the local knowledge base
  /api/files     upload attachments, download deliverables
  /api/system    audit log, network monitor, agent roster, health

The chat endpoint streams Server-Sent Events so the UI can show the plan
before execution, then light each step as it runs. That is what makes the
planning and iteration visible rather than asserted.
"""

from __future__ import annotations

import asyncio
import json
import queue
import threading
import time
import uuid
from pathlib import Path

from fastapi import FastAPI, File, Form, HTTPException, UploadFile
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse, StreamingResponse
from pydantic import BaseModel

from . import agents, composer, executor, manifest, planner, store, supervisor
from .audit import log_event, tail
from .config import DEFAULT_CTX, MAX_REPLANS
from .netmon import monitor
from .rag import index as ragindex, resolver
from .tools import intake
from .tools.files import outputs_dir, session_dir, stage_upload

app = FastAPI(title="Industrial AI Workbench", version="0.1.0")

app.add_middleware(
    CORSMiddleware,
    # The server binds 127.0.0.1 only, so nothing off this machine can reach
    # it whatever the origin says. The packaged app's page origin is
    # tauri://localhost, which no fixed allowlist covers cleanly.
    allow_origin_regex=".*",
    allow_credentials=False,
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.on_event("startup")
def _startup():
    monitor.start()
    log_event("server.start")


@app.on_event("shutdown")
def _shutdown():
    monitor.stop()
    log_event("server.stop")


# ============================================================ MODELS

class PullRequest(BaseModel):
    repo_id: str
    quant: str | None = None
    filename: str | None = None


@app.get("/api/models/browse")
def browse_models(q: str = "", limit: int = 30, sort: str = "downloads"):
    """Hugging Face catalogue. With no query this is the full trending list."""
    from huggingface_hub import HfApi
    api = HfApi()
    kwargs = {"filter": "gguf", "sort": sort, "limit": limit}
    if q:
        kwargs["search"] = q
    out = []
    for m in api.list_models(**kwargs):
        out.append({
            "repo_id": m.id,
            "downloads": getattr(m, "downloads", 0) or 0,
            "likes": getattr(m, "likes", 0) or 0,
            "tags": list(getattr(m, "tags", []) or [])[:10],
        })
    return {"models": out}


@app.get("/api/models/info/{repo_id:path}")
def model_info(repo_id: str, card_chars: int = 8000):
    """Card plus every GGUF file with a fit badge against available RAM."""
    from huggingface_hub import HfApi, hf_hub_download
    api = HfApi()
    try:
        info = api.model_info(repo_id, files_metadata=True)
    except Exception as e:
        raise HTTPException(404, f"cannot read repo: {e}")

    try:
        import psutil
        budget = (psutil.virtual_memory().total / 1024**3 - 6.0) * 0.7
    except ImportError:
        budget = 8.0

    files = []
    for f in info.siblings:
        if not f.rfilename.lower().endswith(".gguf"):
            continue
        gb = (f.size or 0) / 1024**3
        files.append({
            "filename": f.rfilename,
            "size_bytes": f.size,
            "size_gb": round(gb, 2),
            "is_mmproj": "mmproj" in f.rfilename.lower(),
            "fit": "good" if gb < budget * 0.7 else
                   "tight" if gb < budget else "too_large",
        })
    files.sort(key=lambda x: x["size_bytes"] or 0)

    card = ""
    try:
        card = Path(hf_hub_download(repo_id, "README.md")).read_text(
            encoding="utf-8", errors="replace")[:card_chars]
    except Exception:
        pass

    weights = [f for f in files if not f["is_mmproj"] and f["fit"] != "too_large"]
    return {
        "repo_id": repo_id,
        "downloads": getattr(info, "downloads", 0),
        "likes": getattr(info, "likes", 0),
        "tags": list(info.tags or [])[:20],
        "files": files,
        "recommended": weights[-1]["filename"] if weights else None,
        "ram_budget_gb": round(budget, 1),
        "card": card,
    }


@app.post("/api/models/pull")
def pull_model(req: PullRequest):
    """Download weights (+ mmproj + README), then enrich from frontmatter."""
    from huggingface_hub import HfApi, snapshot_download
    from .config import MODELS_DIR

    api = HfApi()
    info = api.model_info(req.repo_id, files_metadata=True)
    ggufs = [f for f in info.siblings if f.rfilename.lower().endswith(".gguf")]
    if not ggufs:
        raise HTTPException(400, "repo contains no .gguf files")

    weights = [f for f in ggufs if "mmproj" not in f.rfilename.lower()]
    if req.filename:
        chosen = next((f for f in weights if f.rfilename == req.filename), None)
    elif req.quant:
        chosen = next((f for f in weights
                       if req.quant.lower() in f.rfilename.lower()), None)
    else:
        try:
            import psutil
            budget = (psutil.virtual_memory().total / 1024**3 - 6.0) * 0.7
        except ImportError:
            budget = 8.0
        fitting = [f for f in weights if (f.size or 0) / 1024**3 < budget]
        chosen = max(fitting, key=lambda f: f.size or 0) if fitting else \
            min(weights, key=lambda f: f.size or float("inf"))

    if not chosen:
        raise HTTPException(400, "no matching weight file")

    mmprojs = [f.rfilename for f in ggufs if "mmproj" in f.rfilename.lower()]
    patterns = [chosen.rfilename, "README.md"] + (mmprojs[:1] if mmprojs else [])

    dest = MODELS_DIR / req.repo_id.replace("/", "__").lower()
    snapshot_download(repo_id=req.repo_id, local_dir=str(dest),
                      allow_patterns=patterns)

    m = manifest.load()
    m[req.repo_id] = {
        "repo_id": req.repo_id,
        "path": str(dest),
        "weight_file": chosen.rfilename,
        "mmproj_file": mmprojs[0] if mmprojs else None,
        "size_bytes": chosen.size,
        "quant": chosen.rfilename,
        "backend": "llama.cpp",
        "vision_capable": bool(mmprojs),
        "capabilities": [],
        "enriched": False,
    }
    manifest.save(m)
    manifest.enrich_all()

    log_event("models.pull", repo_id=req.repo_id, file=chosen.rfilename)
    return {"ok": True, "model": manifest.get(req.repo_id)}


@app.get("/api/models/installed")
def installed_models():
    return {"models": manifest.load(), "running": supervisor.status()}


@app.post("/api/models/enrich")
def enrich_models():
    return {"models": manifest.enrich_all()}


@app.post("/api/models/load/{repo_id:path}")
def load_model(repo_id: str, ctx: int = DEFAULT_CTX):
    try:
        return supervisor.start(repo_id, ctx)
    except Exception as e:
        raise HTTPException(500, str(e))


@app.post("/api/models/unload/{repo_id:path}")
def unload_model(repo_id: str):
    return {"stopped": supervisor.stop(repo_id)}


@app.post("/api/models/unload_all")
def unload_all():
    return {"stopped": supervisor.stop(None)}


@app.delete("/api/models/eject/{repo_id:path}")
def eject_model(repo_id: str):
    """Stop it, delete the files, drop the manifest entry."""
    import shutil
    supervisor.stop(repo_id)
    m = manifest.load()
    entry = m.get(repo_id)
    if not entry:
        raise HTTPException(404, "not installed")
    p = Path(entry["path"])
    if p.exists():
        shutil.rmtree(p)
    m.pop(repo_id)
    manifest.save(m)
    log_event("models.eject", repo_id=repo_id)
    return {"ok": True}


# ============================================================ FILES

@app.post("/api/files/upload")
async def upload(session_id: str = Form(...), file: UploadFile = File(...)):
    d = session_dir(session_id) / "inputs"
    dest = d / file.filename
    dest.write_bytes(await file.read())
    rec = intake.inspect(session_id, dest)

    # Anything with a text layer is chunked and embedded on arrival. This is
    # not optional and no model decides it — by the time the user asks about
    # the document, it is already searchable.
    if rec["kind"] in ("pdf_text", "text"):
        try:
            rec["indexed_chunks"] = ragindex.ingest_file(
                dest, doc=file.filename, session_id=session_id)
        except Exception as e:
            rec["index_error"] = str(e)
            log_event("files.index_failed", session_id=session_id,
                      name=file.filename, error=str(e))

    log_event("files.upload", session_id=session_id, name=file.filename,
              kind=rec["kind"], indexed=rec.get("indexed_chunks", 0))
    return rec


@app.get("/api/files/download")
def download(path: str):
    p = Path(path)
    if not p.exists():
        raise HTTPException(404, "not found")
    return FileResponse(str(p), filename=p.name)


@app.get("/api/files/deliverables/{session_id}")
def deliverables(session_id: str):
    d = outputs_dir(session_id)
    return {"files": [
        {"name": f.name, "path": str(f), "size": f.stat().st_size}
        for f in sorted(d.glob("*")) if f.is_file()
    ]}


# ============================================================ CHAT

class ChatRequest(BaseModel):
    message: str
    conversation_id: str | None = None
    session_id: str | None = None
    files: list[str] = []
    planner_model: str | None = None
    composer_model: str | None = None


def _prepare(req: ChatRequest) -> tuple[str, str, list[dict]]:
    cid = req.conversation_id or store.new_conversation(req.message[:60])
    sid = req.session_id or cid

    paths = []
    if req.files:
        paths = [Path(f) for f in req.files if Path(f).exists()]
    else:
        inputs = session_dir(sid) / "inputs"
        paths = [p for p in sorted(inputs.glob("*")) if p.is_file()]

    inv = intake.build_inventory(sid, paths) if paths else []
    return cid, sid, inv


@app.post("/api/chat/stream")
async def chat_stream(req: ChatRequest):
    """
    Plan, then execute, streaming every event as SSE:
    plan, step.route, step.start, step.progress, step.end, done.
    """
    cid, sid, inv = _prepare(req)
    store.add_message(cid, "user", req.message)

    q: queue.Queue = queue.Queue()

    def worker():
        try:
            history = store.recent_turns(cid, 6)
            request_text = req.message
            if len(history) > 1:
                request_text = ("Conversation so far:\n" + "\n".join(history[:-1])
                                + f"\n\nCurrent request: {req.message}")

            # Deterministic: work out in code which documents the user
            # referred to and where they are, before the planner is asked
            # anything. The planner is told, not left to guess.
            verdict = resolver.resolve_documents(req.message, inv, sid)
            q.put({"type": "resolved", "verdict": verdict})

            plan = planner.make_plan(
                request_text, intake.inventory_for_planner(inv), sid,
                planner_model=req.planner_model,
                doc_status=resolver.describe(verdict), verdict=verdict)
            q.put({"type": "plan", "plan": plan,
                   "chain": planner.describe(plan)})

            # No steps means no work is needed — answer directly instead of
            # dragging a greeting through an agent chain. Attachments are the
            # exception: the conversational path cannot see them, so a
            # follow-up about an image must still run a read step.
            if not plan.get("steps") and not inv and not verdict["in_kb"]:
                q.put({"type": "composing"})
                answer = composer.compose(req.message, {}, model=req.composer_model,
                                          session_id=sid, history=history,
                                          direct=True)
                run_id = uuid.uuid4().hex[:16]
                store.save_run(run_id, cid, req.message, plan, {"direct": True})
                store.add_message(cid, "assistant", answer, {"run_id": run_id})
                q.put({"type": "done", "answer": answer, "run_id": run_id,
                       "conversation_id": cid, "session_id": sid,
                       "deliverables": [], "sources": [], "ok": True,
                       "network": monitor.snapshot()})
                return

            result = executor.run_plan(plan, req.message, inv, sid,
                                       on_event=lambda e: q.put(e))

            # One replan on failure, then stop. Open-ended replanning with a
            # small model produces worse plans, not better ones.
            replans = 0
            while not result["ok"] and replans < MAX_REPLANS:
                replans += 1
                q.put({"type": "replan", "attempt": replans,
                       "reason": result["failed_step"]})
                plan = planner.make_plan(
                    request_text, intake.inventory_for_planner(inv), sid,
                    planner_model=req.planner_model,
                    doc_status=resolver.describe(verdict), verdict=verdict,
                    failure=json.dumps(result["failed_step"]))
                q.put({"type": "plan", "plan": plan,
                       "chain": planner.describe(plan)})
                result = executor.run_plan(plan, req.message, inv, sid,
                                           on_event=lambda e: q.put(e))

            q.put({"type": "composing"})
            answer = composer.compose(req.message, result,
                                      model=req.composer_model,
                                      session_id=sid, history=history)
            run_id = uuid.uuid4().hex[:16]
            store.save_run(run_id, cid, req.message, plan, result)
            store.add_message(cid, "assistant", answer,
                              {"run_id": run_id,
                               "deliverables": result["deliverables"],
                               "sources": result["sources"]})

            q.put({"type": "done", "answer": answer, "run_id": run_id,
                   "conversation_id": cid, "session_id": sid,
                   "deliverables": result["deliverables"],
                   "sources": result["sources"],
                   "ok": result["ok"],
                   "network": monitor.snapshot()})
        except Exception as e:
            log_event("chat.error", session_id=sid, error=str(e))
            q.put({"type": "error", "error": str(e)})
        finally:
            q.put(None)

    threading.Thread(target=worker, daemon=True).start()

    async def gen():
        while True:
            try:
                item = q.get_nowait()
            except queue.Empty:
                await asyncio.sleep(0.05)
                continue
            if item is None:
                break
            yield f"data: {json.dumps(item, default=str)}\n\n"

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@app.post("/api/chat")
def chat(req: ChatRequest):
    """Non-streaming equivalent, for scripting and tests."""
    cid, sid, inv = _prepare(req)
    store.add_message(cid, "user", req.message)

    history = store.recent_turns(cid, 6)
    verdict = resolver.resolve_documents(req.message, inv, sid)
    plan = planner.make_plan(req.message, intake.inventory_for_planner(inv),
                             sid, planner_model=req.planner_model,
                             doc_status=resolver.describe(verdict),
                             verdict=verdict)

    if not plan.get("steps") and not inv and not verdict["in_kb"]:
        answer = composer.compose(req.message, {}, model=req.composer_model,
                                  session_id=sid, history=history, direct=True)
        result = {"direct": True, "ok": True, "deliverables": [], "sources": []}
    else:
        result = executor.run_plan(plan, req.message, inv, sid)
        answer = composer.compose(req.message, result,
                                  model=req.composer_model,
                                  session_id=sid, history=history)

    run_id = uuid.uuid4().hex[:16]
    store.save_run(run_id, cid, req.message, plan, result)
    store.add_message(cid, "assistant", answer,
                      {"run_id": run_id,
                       "deliverables": result.get("deliverables", [])})

    return {"conversation_id": cid, "session_id": sid, "run_id": run_id,
            "plan": plan, "chain": planner.describe(plan),
            "answer": answer, "result": result,
            "network": monitor.snapshot()}


# ============================================================ SESSIONS

@app.get("/api/sessions")
def list_sessions():
    return {"conversations": store.list_conversations()}


@app.post("/api/sessions")
def create_session(title: str = "New chat"):
    return {"conversation_id": store.new_conversation(title)}


@app.get("/api/sessions/{cid}")
def get_session(cid: str):
    return {"conversation_id": cid, "messages": store.get_messages(cid)}


@app.delete("/api/sessions/{cid}")
def del_session(cid: str):
    store.delete_conversation(cid)
    return {"ok": True}


@app.get("/api/runs/{run_id}")
def get_run(run_id: str):
    r = store.get_run(run_id)
    if not r:
        raise HTTPException(404, "no such run")
    return r


# ============================================================ KB

class IngestRequest(BaseModel):
    path: str
    doc_id: str | None = None


@app.post("/api/kb/ingest")
def kb_ingest(req: IngestRequest):
    p = Path(req.path)
    if not p.exists():
        raise HTTPException(404, "file not found")
    n = ragindex.ingest_file(p, req.doc_id)
    return {"ok": True, "chunks": n, "stats": ragindex.stats()}


@app.post("/api/kb/ingest_upload")
async def kb_ingest_upload(file: UploadFile = File(...)):
    from .config import KB_DIR
    dest = KB_DIR / "docs" / file.filename
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(await file.read())
    n = ragindex.ingest_file(dest, file.filename)
    return {"ok": True, "chunks": n, "stats": ragindex.stats()}


@app.get("/api/kb/search")
def kb_search(q: str, top_k: int = 6):
    return {"hits": ragindex.search(q, top_k=top_k)}


@app.get("/api/kb/stats")
def kb_stats():
    return ragindex.stats()


@app.delete("/api/kb")
def kb_clear():
    ragindex.clear()
    return {"ok": True}


# ============================================================ SYSTEM

@app.get("/api/system/agents")
def system_agents():
    return {"available": agents.available(), "unavailable": agents.unavailable()}


@app.get("/api/system/network")
def system_network():
    return monitor.snapshot()


@app.post("/api/system/network/reset")
def system_network_reset():
    monitor.reset()
    return monitor.snapshot()


@app.get("/api/system/audit")
def system_audit(n: int = 200, session_id: str | None = None):
    return {"events": tail(n, session_id)}


@app.get("/api/system/health")
def system_health():
    import platform
    info = {
        "platform": platform.system(),
        "machine": platform.machine(),
        "backend": "llama.cpp",
        "models_installed": len(manifest.load()),
        "models_running": supervisor.status(),
        "agents": [a["agent"] for a in agents.available()],
        "kb": ragindex.stats(),
        "network": monitor.snapshot(),
    }
    try:
        import psutil
        m = psutil.virtual_memory()
        info["ram_total_gb"] = round(m.total / 1024**3, 1)
        info["ram_used_gb"] = round((m.total - m.available) / 1024**3, 1)
    except ImportError:
        pass
    return info


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.server:app", host="127.0.0.1", port=8000, reload=False)
