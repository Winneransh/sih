"""
Executor.

Mechanical. It reads the plan and does exactly what it says — spawning the
declared number of agent runs, respecting parallel-vs-sequential and the
concurrency cap, and assembling outputs according to the stated fan-in rule.

It makes no decisions. When ten Vision Agent runs feed one PPTX Agent, the
executor concatenates their (already bounded) outputs and hands the PPTX
Agent a single assembled input. There is no synthesis agent, because
merging is assembly, not reasoning.

It also enforces two invariants the agents cannot be trusted to maintain:
bulk content stays on disk, and citations survive every handoff.
"""

from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Callable

from . import agents
from .agents.base import AgentInput, AgentResult
from .audit import log_event
from .config import MAX_FANOUT


class ExecutionState:
    """Carries results, artifacts and citations across steps."""

    def __init__(self, session_id: str, request: str, inventory: list[dict]):
        self.session_id = session_id
        self.request = request
        self.inventory = inventory
        self.results: dict[str, list[AgentResult]] = {}
        self.summaries: list[str] = []
        self.sources: list[dict] = []
        self.artifacts: list[str] = []

    def add(self, step_id: str, results: list[AgentResult]) -> None:
        self.results[step_id] = results
        for r in results:
            if r.summary:
                self.summaries.append(f"[{r.agent}] {r.summary}")
            for s in r.sources:
                if s not in self.sources:
                    self.sources.append(s)
            self.artifacts.extend(r.artifacts)

    def context(self) -> dict:
        return {
            "request": self.request,
            "prior_summaries": self.summaries[-10:],
            "sources": self.sources,
        }


def _resolve_inputs(step: dict, state: ExecutionState) -> list[dict]:
    """
    Turn the step's input references into concrete payloads.

    "file:<path>"  -> an intake file record
    "<step_id>"    -> that step's results
    "<step_id>.*"  -> all results of a fanned-out step
    """
    payloads: list[dict] = []

    for ref in step.get("inputs", []):
        if ref.startswith("file:"):
            wanted = ref[5:]
            for item in state.inventory:
                if wanted in (item["path"], item["name"], item["abs_path"]):
                    payloads.append(_file_payload(item))
                    break
            else:
                # Unmatched reference — pass the raw path through and let the
                # agent fail loudly rather than silently doing nothing.
                payloads.append({"path": wanted})
        else:
            base = ref.split(".")[0]
            for r in state.results.get(base, []):
                payloads.append({
                    "from_step": base,
                    "from_agent": r.agent,
                    "summary": r.summary,
                    "data": r.data,
                    "artifacts": r.artifacts,
                    "sources": r.sources,
                    # Provenance travels with the payload. A document agent
                    # binds tightly to extracted material and loosely to
                    # generated material, and cannot tell them apart without
                    # this.
                    "sourced": (r.data or {}).get("sourced", True),
                })

    # No declared inputs: default to every attached file for the first step.
    if not payloads and not state.results:
        payloads = [_file_payload(i) for i in state.inventory]

    return payloads


def _file_payload(item: dict) -> dict:
    p = {"path": item["abs_path"], "name": item["name"], "kind": item["kind"]}
    if item.get("page_images"):
        p["page_images"] = item["page_images"]
        p["images"] = item["page_images"]
    elif item["kind"] == "image":
        p["images"] = [item["abs_path"]]
    return p


def _fan_in(payloads: list[dict], rule: str) -> dict:
    """Assemble many payloads into one, per the planner's stated rule."""
    if rule == "first_only":
        return payloads[0] if payloads else {}

    if rule == "merge_findings":
        findings, sources = [], []
        for p in payloads:
            d = p.get("data") or {}
            findings.extend(d.get("findings", []) or [])
            sources.extend(p.get("sources", []) or [])
        return {"inputs": payloads, "findings": findings, "sources": sources,
                "n_inputs": len(payloads)}

    if rule == "group_by_source":
        groups: dict[str, list] = {}
        for p in payloads:
            key = (p.get("sources") or [{}])[0].get("doc", "unknown")
            groups.setdefault(key, []).append(p.get("data") or p.get("summary"))
        return {"groups": groups, "n_inputs": len(payloads),
                "sources": [s for p in payloads for s in (p.get("sources") or [])]}

    # concat (default)
    return {"inputs": payloads, "n_inputs": len(payloads),
            "sourced": all(p.get("sourced", True) for p in payloads),
            "sources": [s for p in payloads for s in (p.get("sources") or [])]}


def _run_one(agent, session_id: str, step: dict, payload: dict,
             context: dict) -> AgentResult:
    return agent.run(AgentInput(
        session_id=session_id,
        task=step.get("task", ""),
        payload=payload,
        model=step.get("model"),
        context=context,
        options=step.get("options", {}) or {},
    ))


def execute_step(step: dict, state: ExecutionState,
                 on_event: Callable[[dict], None] | None = None) -> list[AgentResult]:
    agent = agents.get(step["agent"])
    payloads = _resolve_inputs(step, state)
    card = step["cardinality"]
    context = state.context()

    def emit(kind: str, **kw):
        if on_event:
            on_event({"type": kind, "step": step["id"],
                      "agent": step["agent"], **kw})

    # N->1 : assemble first, then a single run.
    if card == "N->1":
        merged = _fan_in(payloads, step.get("fan_in", "concat"))
        emit("step.start", n_runs=1, mode="N->1", n_inputs=len(payloads))
        return [_run_one(agent, state.session_id, step, merged, context)]

    # 1->1 : one run over whatever came in.
    if card == "1->1":
        payload = payloads[0] if len(payloads) == 1 else _fan_in(payloads, "concat")
        emit("step.start", n_runs=1, mode="1->1")
        return [_run_one(agent, state.session_id, step, payload, context)]

    # N->N and 1->N : one run per input.
    if card == "1->N":
        # Split a single input's pages into separate runs.
        src = payloads[0] if payloads else {}
        images = src.get("page_images") or src.get("images") or []
        payloads = [{**src, "images": [im], "page_images": [im]} for im in images] \
            or [src]

    payloads = payloads[:MAX_FANOUT]
    n = len(payloads)
    parallel = step.get("execution") == "parallel"
    cap = max(1, int(step.get("max_parallel", 3)))

    emit("step.start", n_runs=n, mode=card,
         execution=step.get("execution"), max_parallel=cap)

    results: list[AgentResult] = []

    if parallel and n > 1:
        with ThreadPoolExecutor(max_workers=cap) as pool:
            futures = {
                pool.submit(_run_one, agent, state.session_id, step, p, context): i
                for i, p in enumerate(payloads)
            }
            ordered: dict[int, AgentResult] = {}
            for fut in as_completed(futures):
                i = futures[fut]
                ordered[i] = fut.result()
                emit("step.progress", done=len(ordered), total=n)
            results = [ordered[i] for i in sorted(ordered)]
    else:
        for i, p in enumerate(payloads):
            results.append(_run_one(agent, state.session_id, step, p, context))
            emit("step.progress", done=i + 1, total=n)

    return results


def run_plan(plan: dict, request: str, inventory: list[dict],
             session_id: str,
             on_event: Callable[[dict], None] | None = None) -> dict:
    state = ExecutionState(session_id, request, inventory)
    failed_step = None

    for step in plan.get("steps", []):
        if on_event:
            on_event({"type": "step.route", "step": step["id"],
                      "agent": step["agent"], "model": step.get("model"),
                      "cardinality": step["cardinality"],
                      "execution": step.get("execution"),
                      "task": step.get("task", "")[:200]})

        results = execute_step(step, state, on_event)
        state.add(step["id"], results)

        ok = any(r.ok for r in results)
        if on_event:
            on_event({"type": "step.end", "step": step["id"],
                      "agent": step["agent"], "ok": ok,
                      "summaries": [r.summary for r in results][:5],
                      "artifacts": [a for r in results for a in r.artifacts]})

        if not ok:
            failed_step = {
                "step": step["id"], "agent": step["agent"],
                "errors": [r.error for r in results if r.error][:3],
            }
            log_event("executor.step_failed", session_id=session_id, **failed_step)
            break

    # Deliverables are what the user actually receives — outputs only,
    # not intermediate spill files.
    deliverables = [a for a in state.artifacts if "/outputs/" in a.replace("\\", "/")]

    return {
        "session_id": session_id,
        "ok": failed_step is None,
        "failed_step": failed_step,
        "summaries": state.summaries,
        "sources": state.sources,
        "artifacts": state.artifacts,
        "deliverables": deliverables,
        "results": {k: [r.to_dict() for r in v] for k, v in state.results.items()},
    }
