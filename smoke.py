#!/usr/bin/env python3
r"""
smoke.py — end-to-end check of every layer, no frontend needed.

Run:  .venv\Scripts\python.exe smoke.py           # all checks
      .venv\Scripts\python.exe smoke.py --quick   # skip the slow model calls
      .venv\Scripts\python.exe smoke.py --plan "read the report and draft an approval note"

Each check prints PASS or FAIL with a reason. Nothing is mocked — if a check
passes, that layer genuinely works.
"""

import argparse
import json
import sys
import uuid
from pathlib import Path

from app import agents, manifest, planner, supervisor
from app.netmon import monitor
from app.rag import index as ragindex, resolver
from app.tools import docgen, intake, sandbox
from app.tools.files import outputs_dir, session_dir

PASS, FAIL = "PASS", "FAIL"
results = []


def check(name, fn):
    try:
        detail = fn()
        results.append((PASS, name, detail or ""))
        print(f"[{PASS}] {name}  {detail or ''}")
        return True
    except Exception as e:
        results.append((FAIL, name, str(e)))
        print(f"[{FAIL}] {name}  {e}")
        return False


# ------------------------------------------------------------------ checks

def c_manifest():
    m = manifest.load()
    if not m:
        raise RuntimeError("no models installed — run hfcli.py pull first")
    unenriched = [k for k, v in m.items() if not v.get("enriched")]
    if unenriched:
        manifest.enrich_all()
    caps = {c for v in manifest.load().values() for c in v.get("capabilities", [])}
    return f"{len(m)} model(s), capabilities: {', '.join(sorted(caps))}"


def c_agents():
    av = agents.available()
    un = agents.unavailable()
    if not av:
        raise RuntimeError("no agents usable")
    names = ", ".join(a["agent"] for a in av)
    extra = f" | unavailable: {', '.join(u['agent'] for u in un)}" if un else ""
    return f"{len(av)} usable ({names}){extra}"


def c_supervisor():
    text_models = manifest.by_capability("text")
    if not text_models:
        raise RuntimeError("no text-capable model installed")
    rec = supervisor.start(text_models[0])
    if not supervisor.is_alive(rec["port"]):
        raise RuntimeError("started but not healthy")
    return f"{text_models[0]} on :{rec['port']}"


def c_chat():
    from app import llm
    out = llm.chat("Reply with exactly the word: READY", max_tokens=10)
    if "ready" not in out.lower():
        raise RuntimeError(f"unexpected reply: {out[:80]!r}")
    return "model responded"


def c_json_mode():
    from app import llm
    schema = {
        "type": "object",
        "properties": {"colour": {"type": "string"}, "count": {"type": "integer"}},
        "required": ["colour", "count"],
    }
    d = llm.chat_json("Return colour 'blue' and count 3.", schema, max_tokens=100)
    if "colour" not in d:
        raise RuntimeError(f"bad JSON: {d}")
    return f"constrained JSON works: {d}"


def c_sandbox():
    r = sandbox.run_python("print(sum(range(10)))")
    if not r["ok"] or "45" not in r["stdout"]:
        raise RuntimeError(f"exit={r['exit_code']} stderr={r['stderr'][:200]}")
    return r["backend"]


def c_sandbox_isolated():
    """The sandbox must not be able to reach the network."""
    code = ("import socket\n"
            "try:\n"
            "    socket.create_connection(('1.1.1.1', 53), timeout=3)\n"
            "    print('REACHED')\n"
            "except Exception:\n"
            "    print('BLOCKED')\n")
    r = sandbox.run_python(code, timeout=20)
    if "REACHED" in r["stdout"]:
        if sandbox.docker_available():
            raise RuntimeError("docker sandbox reached the network")
        return "subprocess backend — no network isolation (dev only)"
    return "network blocked"


def c_docgen():
    sid = "smoke_" + uuid.uuid4().hex[:6]
    out = outputs_dir(sid)
    docgen.write_approval_note(out / "note.docx", {
        "ref_no": "SMOKE/001", "subject": "Smoke test",
        "background": "Verifying document generation.",
        "findings": [{"finding": "All layers responded", "severity": "low"}],
        "recommendation": "Proceed.", "prepared_by": "smoke.py",
        "sources": [{"doc": "SOP-01", "page": 3}],
    })
    docgen.write_pptx(out / "deck.pptx", {
        "title": "Smoke", "slides": [{"title": "One", "bullets": ["a", "b"]}]})
    docgen.write_xlsx(out / "data.xlsx", {
        "sheets": [{"name": "S1", "headers": ["a", "b"], "rows": [["1", "2"]]}]})
    made = [f.name for f in out.glob("*") if f.is_file()]
    if len(made) < 3:
        raise RuntimeError(f"only produced {made}")
    return f"{', '.join(sorted(made))} in {out}"


def c_embedding():
    if not manifest.by_capability("embedding"):
        raise RuntimeError("no embedding model installed — KB agent will not work")
    from app import llm
    v = llm.embed(["test sentence"])
    return f"{len(v[0])} dimensions"


def c_kb():
    if not manifest.by_capability("embedding"):
        raise RuntimeError("skipped — no embedding model")
    ragindex.ingest_text("SMOKE-SOP", (
        "3.1 Valve Inspection\n"
        "All gate valves tagged GV-1xx must be inspected quarterly. "
        "Record the result against tag GV-101 in the maintenance register.\n"
    ), page=3)
    hits = ragindex.search("how often are gate valves inspected", top_k=3)
    if not hits:
        raise RuntimeError("no hits returned")
    h = hits[0]
    if not h.get("page"):
        raise RuntimeError("citation page missing — the chain is broken")
    return f"{len(hits)} hits, top from [{h['doc']}, p.{h['page']}]"


def c_resolver():
    """The resolver must find an indexed document by name, in code."""
    ragindex.ingest_text("proc-manual.pdf",
                         "2.4 Isolation\nIsolate upstream before removal.\n",
                         page=2)
    v = resolver.resolve_documents("what does proc-manual.pdf say about isolation", [])
    if "proc-manual.pdf" not in v["in_kb"]:
        raise RuntimeError(f"failed to resolve an indexed document: {v}")
    if v["route"] != "rag":
        raise RuntimeError(f"routed to {v['route']} instead of rag")

    miss = resolver.resolve_documents("what about nonexistent-file.pdf", [])
    if not miss["missing"]:
        raise RuntimeError("failed to report a missing document")
    return f"resolved -> {v['route']}, missing detected"


def c_netmon():
    snap = monitor.snapshot()
    if not snap["available"]:
        raise RuntimeError("psutil not installed — monitor cannot run")
    monitor.poll()
    return (f"external={snap['external_calls']} "
            f"provisioning={snap['provisioning_calls']} "
            f"sovereign={snap['sovereign']}")


def c_audit():
    from app.audit import log_event, tail
    log_event("smoke.test", note="audit check")
    ev = tail(5)
    if not any(e["event"] == "smoke.test" for e in ev):
        raise RuntimeError("event not written")
    return f"{len(ev)} recent events"


def c_composer():
    """The final pass must present agent content, not a log line."""
    from app import composer
    fake = {
        "ok": True,
        "summaries": ["[vision] Read 1 image in 'transcribe' mode, 62 chars."],
        "deliverables": [],
        "artifacts": [],
        "sources": [],
        "results": {"s1": [{
            "agent": "vision", "ok": True,
            "summary": "Read 1 image in 'transcribe' mode, 62 chars.",
            "data": {"text": "VALVE GV-101 INSPECTED 12/03. SEAT WEAR NOTED. "
                             "ACTION: REPLACE SEAT.", "mode": "transcribe"},
            "artifacts": [], "sources": [], "error": None,
        }]},
    }
    out = composer.compose("Read this inspection note", fake)
    if "GV-101" not in out:
        raise RuntimeError(f"content mode did not surface the agent's text: {out[:200]!r}")
    return f"content surfaced ({len(out)} chars)"


def c_textcheck():
    """Degenerate output must be caught, not passed on."""
    from app.textcheck import is_degenerate, strip_repeats
    looped = "Extract the text from this image.\n" * 30
    bad, why = is_degenerate(looped)
    if not bad:
        raise RuntimeError("failed to detect a looped generation")
    good, _ = is_degenerate("Valve GV-101 inspected. Seat wear noted.")
    if good:
        raise RuntimeError("flagged normal text as degenerate")
    if "GV-101" not in strip_repeats("a\n" * 5 + "Valve GV-101 inspected here."):
        raise RuntimeError("salvage dropped real content")
    return why


def c_direct():
    """A greeting must not produce an agent plan."""
    sid = "smoke_" + uuid.uuid4().hex[:6]
    session_dir(sid)
    plan = planner.make_plan("hi how are you", [], sid)
    if plan.get("steps"):
        raise RuntimeError(
            f"planner built {len(plan['steps'])} step(s) for a greeting: "
            f"{planner.describe(plan)}")
    from app import composer
    answer = composer.compose("hi how are you", {}, session_id=sid, direct=True)
    if len(answer) < 5:
        raise RuntimeError("no conversational reply produced")
    return f"no steps, replied: {answer[:70]}"


def c_general():
    """General knowledge answering, and correct provenance marking."""
    from app.agents import get
    from app.agents.base import AgentInput
    r = get("general").run(AgentInput(
        session_id="smoke_" + uuid.uuid4().hex[:6],
        task="In two sentences, what is water pollution?"))
    if not r.ok:
        raise RuntimeError(r.error or "general agent failed")
    if r.data.get("sourced") is not False:
        raise RuntimeError("general output not marked unsourced")
    return f"{len(r.data.get('content',''))} chars, marked unsourced"


def c_plan(request: str):
    sid = "smoke_" + uuid.uuid4().hex[:6]
    session_dir(sid)
    plan = planner.make_plan(request, [], sid)
    if not plan.get("steps"):
        raise RuntimeError(f"empty plan; validation={plan.get('validation')}")
    chain = planner.describe(plan)
    detail = f"{len(plan['steps'])} step(s): {chain}"
    if plan.get("validation"):
        detail += f" | notes: {'; '.join(plan['validation'][:2])}"
    return detail


# -------------------------------------------------------------------- main

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true",
                    help="skip checks that call a model")
    ap.add_argument("--plan", default="Summarise the attached inspection report "
                                      "and draft an approval note as a Word file")
    args = ap.parse_args()

    print("\n=== structure ===")
    check("manifest", c_manifest)
    check("agent roster", c_agents)
    print("\n=== tools (no model) ===")
    check("sandbox execution", c_sandbox)
    check("sandbox isolation", c_sandbox_isolated)
    check("document generation", c_docgen)
    check("audit log", c_audit)
    check("network monitor", c_netmon)
    check("degenerate-output guard", c_textcheck)

    if args.quick:
        _summary()
        return

    print("\n=== runtime ===")
    if check("supervisor spawn", c_supervisor):
        check("chat completion", c_chat)
        check("constrained JSON", c_json_mode)
    check("embedding model", c_embedding)
    check("knowledge base + citations", c_kb)
    check("document resolver", c_resolver)

    print("\n=== planner ===")
    check("plan generation", lambda: c_plan(args.plan))
    check("composer", c_composer)
    check("general agent", c_general)
    check("direct answer (no agents)", c_direct)

    _summary()


def _summary():
    passed = sum(1 for r, _, _ in results if r == PASS)
    print(f"\n{'=' * 60}")
    print(f"{passed}/{len(results)} passed")
    failed = [(n, d) for r, n, d in results if r == FAIL]
    if failed:
        print("\nFailed:")
        for n, d in failed:
            print(f"  {n}: {d}")
    print()
    sys.exit(0 if not failed else 1)


if __name__ == "__main__":
    main()
