"""
Planner.

The only component that makes real decisions. It runs once, before anything
executes, and produces the whole plan upfront.

Per step it decides:
  - which agent
  - which specific model that agent uses (chosen from the manifest)
  - cardinality: 1->1, 1->N, N->N, N->1
  - parallel or sequential, and how many at a time
  - how multiple outputs combine before the next step

This is the design's central claim. A general agent harness lets a frontier
model plan freely at runtime, hold enormous context and recover from its own
mistakes mid-flight. Local models under 100B cannot do that reliably. So the
planning happens once, over a closed set of agents and installed models, and
the mechanics are then executed by code that does no reasoning at all.

The plan is validated before execution: unknown agents, bad step references
and impossible cardinalities are rejected before any model is called.
"""

from __future__ import annotations

import json

from . import agents, llm, manifest
from .audit import log_event
from .config import (
    AGENT_CAPABILITY, DEFAULT_MAX_PARALLEL, MAX_FANOUT, MAX_PLAN_STEPS,
)

CARDINALITIES = ["1->1", "1->N", "N->N", "N->1"]
FAN_IN = ["concat", "merge_findings", "group_by_source", "first_only"]

PLAN_SCHEMA = {
    "type": "object",
    "properties": {
        "reasoning": {"type": "string"},
        "needs_work": {"type": "boolean"},
        "deliverable": {"type": "string"},
        "steps": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "id": {"type": "string"},
                    "agent": {"type": "string"},
                    "model": {"type": "string"},
                    "task": {"type": "string"},
                    "cardinality": {"type": "string", "enum": CARDINALITIES},
                    "inputs": {"type": "array", "items": {"type": "string"}},
                    "execution": {"type": "string", "enum": ["parallel", "sequential"]},
                    "max_parallel": {"type": "integer"},
                    "fan_in": {"type": "string", "enum": FAN_IN},
                    "options": {
                        "type": "object",
                        "properties": {
                            "mode": {"type": "string"},
                            "template": {"type": "string"},
                            "filename": {"type": "string"},
                        },
                    },
                },
                "required": ["id", "agent", "model", "task", "cardinality",
                             "inputs", "execution", "fan_in"],
            },
        },
    },
    "required": ["reasoning", "needs_work", "deliverable", "steps"],
}

SYSTEM = """You are the planner for a local, self-hosted AI workbench used in industrial and government settings.

You do not do the work. You decide which specialist agents run, in what order, on how many inputs, using which installed model.

FIRST, decide whether any work is needed at all.

Set "needs_work": false and return an EMPTY steps list ONLY when the message
is pure conversation — a greeting, thanks, small talk, or a question about
what this system can do. The assistant will simply reply. A plan for "hello"
is worse than no plan.

Set "needs_work": true whenever something must be read, searched, computed,
run or written to a file.

IMPORTANT: if any files are attached, needs_work is ALWAYS true, even for a
short follow-up like "tell me more" or "what about the top left". The
assistant cannot see attachments on its own — only an agent can read them. A
follow-up question about an attached image still needs a vision step.

For each step you must decide:

1. agent      — from the roster given to you. Never invent one.
2. model      — an exact model name from the installed models list. Match the
                agent's required capability. Prefer a larger model for hard
                visual work, a smaller one for simple text.
3. cardinality:
     "1->1"  one input, one output
     "1->N"  one input split into many outputs
     "N->N"  many inputs, one agent run per input (this is how 10 documents
             become 10 runs — you declare it once, not ten times)
     "N->1"  many inputs consolidated into a single run
4. execution  — "parallel" when the runs are independent, "sequential" when
                each depends on the previous, or when running many at once
                would overwhelm the local machine.
5. max_parallel — how many concurrent runs when parallel. Keep it to 2-4 on
                local hardware.
6. fan_in     — for N->1, how the many outputs are combined before the agent
                sees them: "concat", "merge_findings", "group_by_source",
                "first_only".
7. inputs     — either "file:<path>" for an intake file, or "<step_id>" to
                consume an earlier step's output, or "<step_id>.*" for all
                outputs of a fanned-out step.

Rules:
- Keep the plan as short as the task genuinely needs. A simple question is
  ONE step. Do not manufacture steps to look thorough.
- The DOCUMENT STATUS block above is established fact, checked in code
  before you were asked. Follow it exactly. If it says a document is indexed,
  plan a rag step and do not plan a vision step for it. If it says a document
  was not found, say so instead of planning a step that cannot succeed.
- ANY question about procedures, specifications, limits, equipment, past
  correspondence, or anything the organisation would have written down goes
  through the rag agent. Never answer these from your own knowledge — you do
  not have the organisation's documents.
- A knowledge question is normally ONE rag step and nothing else. The rag
  agent searches several ways, discards what does not apply, and writes the
  answer. Do not add a document step unless a file was asked for.
- The vision agent is for images and scanned pages only. A document that can
  be read as text is never sent to it.
- The general agent answers and writes from the model's own knowledge, on any
  subject. Use it when the answer is not in the organisation's documents and
  not in an attached file. It is NOT a substitute for the rag agent — if the
  question is about the organisation's own procedures or documents, use rag
  even when the general agent could produce something plausible.
- When a document, deck or spreadsheet is asked for on a subject with no
  source material and no attachment, plan general first and the document
  agent second. A document agent given nothing to work from produces nothing.
  Set options.format to "outline" for a deck, "sections" for a document, or
  "table" for a spreadsheet, so the content arrives in a usable shape.
- The vision agent reads an image completely in one pass — text, tables,
  labels and description together. Do not plan separate steps for reading
  text and describing the same image. Set options.structured=true only when
  a later step needs machine-readable fields rather than prose.
- Scanned or image files must go through the vision agent before any agent
  that produces a document.
- Only add a document agent (docx/pptx/xlsx) when a file deliverable is
  actually wanted.
- Reference only step ids you have already defined earlier in the list.

Return JSON only."""


def _prompt(request: str, inventory: list[dict],
            roster: list[dict], models: list[dict],
            doc_status: str = "",
            failure: str | None = None) -> str:
    parts = [
        f"USER REQUEST:\n{request}",
    ]
    # The resolver has already established, in code, which documents exist and
    # where. This is fact, not something to reason about.
    if doc_status:
        parts.append(f"\n{doc_status}")
    parts += [
        f"\nFILES ATTACHED ({len(inventory)}):\n{json.dumps(inventory, indent=2)}"
        if inventory else "\nFILES ATTACHED: none",
        f"\nAVAILABLE AGENTS:\n{json.dumps(roster, indent=2)}",
        f"\nINSTALLED MODELS:\n{json.dumps(models, indent=2)}",
    ]
    if failure:
        parts.append(
            f"\nA PREVIOUS PLAN FAILED:\n{failure}\n"
            "Produce a different plan that avoids this failure."
        )
    parts.append("\nProduce the plan.")
    return "\n".join(parts)


def _validate(plan: dict, verdict: dict | None = None) -> tuple[dict, list[str]]:
    """Repair what can be repaired, reject what cannot."""
    problems: list[str] = []
    roster = {a["agent"] for a in agents.available()}
    installed = {m["model"] for m in manifest.for_planner()}

    steps = plan.get("steps") or []

    # An empty plan is valid when the planner decided no work is needed —
    # the request gets a direct conversational answer instead.
    if not steps:
        if plan.get("needs_work") is False:
            plan["steps"] = []
            return plan, problems
        problems.append("plan has no steps")
        plan["needs_work"] = False       # fall back to answering directly
        return plan, problems

    if len(steps) > MAX_PLAN_STEPS:
        steps = steps[:MAX_PLAN_STEPS]
        problems.append(f"plan truncated to {MAX_PLAN_STEPS} steps")

    seen: set[str] = set()
    clean = []
    for i, s in enumerate(steps):
        sid = s.get("id") or f"s{i+1}"
        s["id"] = sid

        agent = s.get("agent")
        if agent not in roster:
            problems.append(f"step {sid}: unknown or unavailable agent '{agent}'")
            continue

        # A wrong model name is recoverable — the router falls back on capability.
        if s.get("model") not in installed:
            problems.append(
                f"step {sid}: model '{s.get('model')}' not installed, "
                f"router will substitute by capability")
            s["model"] = None

        if s.get("cardinality") not in CARDINALITIES:
            s["cardinality"] = "1->1"
        if s.get("execution") not in ("parallel", "sequential"):
            s["execution"] = "sequential"
        if s.get("fan_in") not in FAN_IN:
            s["fan_in"] = "concat"

        s["max_parallel"] = max(1, min(int(s.get("max_parallel") or
                                           DEFAULT_MAX_PARALLEL), 8))

        refs = []
        for ref in (s.get("inputs") or []):
            if ref.startswith("file:"):
                refs.append(ref)
            else:
                base = ref.split(".")[0]
                if base in seen:
                    refs.append(ref)
                else:
                    problems.append(
                        f"step {sid}: references unknown step '{ref}', dropped")
        s["inputs"] = refs
        s.setdefault("options", {})

        # Scope retrieval to the documents the resolver identified. Searching
        # the rest of the corpus when the document is already known only adds
        # noise.
        if s["agent"] == "rag" and verdict and verdict.get("in_kb"):
            s["options"].setdefault("documents", verdict["in_kb"])

        seen.add(sid)
        clean.append(s)

    plan["steps"] = clean
    if not clean:
        problems.append("no valid steps survived validation")
        plan["needs_work"] = False
    return plan, problems


def make_plan(request: str,
              inventory: list[dict],
              session_id: str,
              planner_model: str | None = None,
              doc_status: str = "",
              verdict: dict | None = None,
              failure: str | None = None) -> dict:
    roster = agents.available()
    models = manifest.for_planner()

    if not roster:
        raise RuntimeError(
            "no agents are usable — no models installed. Download a model "
            "and run enrichment first.")

    prompt = _prompt(request, inventory, roster, models, doc_status, failure)

    plan = llm.chat_json(prompt, PLAN_SCHEMA, system=SYSTEM,
                         model=planner_model, capability="text",
                         session_id=session_id, max_tokens=2500,
                         temperature=0.2)

    plan, problems = _validate(plan, verdict)

    # A small model will still choose "just chat" for a terse follow-up about
    # an attachment. Attachments are evidence that work is required, so the
    # decision is overridden rather than trusted.
    if (inventory or (verdict or {}).get("in_kb")) and not plan.get("steps"):
        plan = _fallback_plan(inventory, request, roster, models, verdict)
        problems.append("planner returned no steps despite attachments; "
                        "substituted a read step")

    plan["validation"] = problems
    plan["session_id"] = session_id

    log_event("planner.plan", session_id=session_id,
              n_steps=len(plan.get("steps", [])),
              deliverable=plan.get("deliverable"),
              problems=problems,
              chain=[f"{s['agent']}({s['cardinality']})" for s in plan["steps"]])
    return plan


def _fallback_plan(inventory: list[dict], request: str,
                   roster: list[dict], models: list[dict],
                   verdict: dict | None = None) -> dict:
    """
    Minimal plan for when the planner declines work that the request plainly
    requires. Indexed documents are read with rag; attachments the resolver
    could not index are read with vision.
    """
    names = {a["agent"] for a in roster}

    if verdict and verdict.get("in_kb") and "rag" in names:
        text_models = [m["model"] for m in models if "text" in m["capabilities"]]
        return {
            "reasoning": "The named document is indexed, so it is searched "
                         "rather than looked at.",
            "needs_work": True, "deliverable": "answer",
            "steps": [{
                "id": "s1", "agent": "rag",
                "model": text_models[0] if text_models else None,
                "task": request[:300], "cardinality": "1->1",
                "inputs": [], "execution": "sequential", "max_parallel": 1,
                "fan_in": "concat",
                "options": {"documents": verdict["in_kb"]},
            }],
        }

    if "vision" not in names:
        return {"reasoning": "attachments present but no vision model installed",
                "needs_work": False, "deliverable": "answer", "steps": []}

    vision_models = [m["model"] for m in models if "vision" in m["capabilities"]]
    refs = [f"file:{i['path']}" for i in inventory]

    return {
        "reasoning": "Attachments are present, so they must be read before "
                     "the question can be answered.",
        "needs_work": True,
        "deliverable": "answer",
        "steps": [{
            "id": "s1",
            "agent": "vision",
            "model": vision_models[0] if vision_models else None,
            "task": request[:300],
            "cardinality": "N->N" if len(refs) > 1 else "1->1",
            "inputs": refs,
            "execution": "sequential",
            "max_parallel": 1,
            "fan_in": "concat",
            "options": {},
        }],
    }


def describe(plan: dict) -> str:
    """One-line human-readable chain, for the UI trace panel."""
    if not plan.get("steps"):
        return "direct answer"
    bits = []
    for s in plan.get("steps", []):
        n = "" if s["cardinality"] in ("1->1", "1->N") else f" x{s['cardinality']}"
        bits.append(f"{s['agent']}{n}")
    return " -> ".join(bits)
