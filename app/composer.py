"""
Composer.

The last step before anything reaches the screen. It takes the original
request and what the agents produced, and writes the reply.

Without it the user sees a log — "[vision] Read 3 image(s), 4820 chars" —
rather than the transcription they asked for.

Three modes, decided by what the run produced:

  direct    no work was needed (a greeting, a question about the system).
            Plain conversation, no agent output involved.

  content   work ran but produced no file, so the agent output IS the answer.
            The composer reads the full output, pulling spilled content back
            off disk, and answers from it.

  report    a document was produced. The composer says what was done and
            names the file, without restating the document's contents.

It cleans before it composes. Small models loop and echo instructions back;
that output looks like content and would otherwise flow straight into a
deliverable. Degenerate material is stripped or dropped here, and if nothing
usable survives the composer says so rather than presenting noise.

It never adds facts. Everything it writes comes from what the agents
returned — an invented finding in an approval note is worse than no note.
"""

from __future__ import annotations

import json
from pathlib import Path

from . import llm
from .audit import log_event
from .textcheck import is_degenerate, strip_repeats

MAX_MATERIAL_CHARS = 12000

SYSTEM_TRIM = """An answer has already been written from source documents. Return it,
tightened.

Your job is editorial only:
- Remove any preamble, restatement of the question, or closing offer of help.
- Remove a sentence that repeats one already made.
- Leave every fact, figure, identifier and citation exactly as written.

Do NOT rewrite, reorganise, expand, or add anything. If the answer is already
clean, return it unchanged. Return only the answer text."""


SYSTEM_ANSWER = """You answer the user's question using the passages provided.

Write a real answer, in prose, the way a knowledgeable colleague would reply.
Not a report. No headings, no "Findings:", no numbered list unless the answer
is genuinely a list of things.

Rules:
- Every statement must come from a passage. Never use outside knowledge and
  never fill a gap with what is usually true.
- Cite inline as [document, p.N] after the statement it supports.
- Quote exact values, tags, limits and figures as written — do not round or
  paraphrase a number.
- Answer the question that was asked. Do not summarise everything retrieved.
- If the passages only partly answer it, say what they do cover and what is
  missing.
- If they do not answer it at all, say so plainly in one sentence. Do not
  pad it out.
- No preamble, no closing offer of further help."""


SYSTEM_CONTENT = """You present the results of work already done by specialist agents.

Rules:
- Use ONLY what the agents produced. Never add a fact, finding, number or
  interpretation of your own, and never fill a gap with what is plausible.
- When an agent extracted text and the user asked to read something, present
  that text. Do not summarise it away.
- Present structured data (findings, tags, tables) with its structure intact.
- Keep citations only where the agents supplied them. Never invent one.
- If something the user asked about is not in the material, say so plainly
  rather than answering around it.
- If the material is garbled or repetitive, say the model's output was
  unreliable rather than reproducing the noise.
- No preamble, no closing offer of further help. Start with the content."""

SYSTEM_REPORT = """You write a short closing note about work already completed.

Rules:
- State what was done and what was produced, in two or three sentences.
- Use ONLY what the agents reported. Never invent findings or figures, and
  never describe content you were not shown.
- Name the files produced.
- Do not restate the contents of a generated document — the user has the file.
- Mention any step that failed.
- No preamble, no closing offer of further help."""


# ------------------------------------------------------------------ cleaning

def _clean(text: str) -> tuple[str, str | None]:
    """
    Returns (usable_text, reason_dropped).

    Tries to salvage looped output before discarding it — partially degraded
    generations often contain real content before they start repeating.
    """
    if not text or not text.strip():
        return "", "empty"

    bad, why = is_degenerate(text)
    if not bad:
        return text, None

    salvaged = strip_repeats(text)
    still_bad, why2 = is_degenerate(salvaged)
    if still_bad or len(salvaged) < 20:
        return "", why
    return salvaged, None


def _read_spill(data: dict, limit: int) -> str:
    """Agents spill bulk output to disk. When it IS the answer, read it back."""
    if not isinstance(data, dict) or not data.get("_spilled"):
        return ""
    p = Path(data.get("_path", ""))
    if not p.exists():
        return data.get("_preview", "")
    return p.read_text(encoding="utf-8", errors="replace")[:limit]


def _artifact_text(artifacts: list[str], limit: int) -> str:
    """Vision and calc write their full output alongside the summary."""
    out = []
    for a in artifacts:
        p = Path(a)
        if p.suffix.lower() in (".txt", ".json", ".md") and p.exists():
            out.append(p.read_text(encoding="utf-8", errors="replace")[:limit])
    return "\n\n".join(out)


def _material(result: dict, full: bool) -> tuple[str, list[str]]:
    """
    Assemble what the composer sees, cleaning as it goes.

    full=True  pulls actual content back off disk — used when the agent
               output is itself the answer.
    full=False uses summaries only — enough to describe what was produced.

    Returns (material, dropped_reasons).
    """
    parts, dropped = [], []
    budget = MAX_MATERIAL_CHARS

    for step_id, results in result.get("results", {}).items():
        for r in results:
            agent = r.get("agent", "?")
            parts.append(f"--- {agent} ({step_id}) ---")

            summary, why = _clean(r.get("summary") or "")
            if summary:
                parts.append(summary)
            elif why:
                dropped.append(f"{agent} summary: {why}")

            if not full:
                continue

            data = r.get("data") or {}

            raw = _read_spill(data, budget)
            if not raw and data:
                raw = json.dumps(data, indent=2, default=str)[:budget]
            if not raw and budget > 1000:
                raw = _artifact_text(r.get("artifacts", []), budget)

            if raw:
                cleaned, why = _clean(raw)
                if cleaned:
                    parts.append(cleaned)
                    budget -= len(cleaned)
                elif why:
                    dropped.append(f"{agent} output: {why}")

            if budget <= 0:
                break

    return "\n\n".join(p for p in parts if p)[:MAX_MATERIAL_CHARS], dropped


def _written_answer(result: dict) -> str:
    """
    An answer an agent already wrote with its sources in front of it.

    The general agent's output is deliberately excluded: it is model
    knowledge, and the trim path is for answers that were written against
    retrieved passages.
    """
    for results in result.get("results", {}).values():
        for r in results:
            a = (r.get("data") or {}).get("answer")
            if a and str(a).strip():
                return str(a).strip()
    return ""


def _only_general(result: dict) -> str:
    """
    Content the general agent produced, when that is the whole run.

    Returned as-is. The composer has nothing to add to it and no passages to
    check it against — regenerating would only dilute it.
    """
    agents, content = set(), ""
    for results in result.get("results", {}).values():
        for r in results:
            agents.add(r.get("agent"))
            data = r.get("data") or {}
            if r.get("agent") == "general" and data.get("content"):
                content = str(data["content"]).strip()
    return content if agents == {"general"} else ""


def _has_passages(result: dict) -> bool:
    for results in result.get("results", {}).values():
        for r in results:
            if (r.get("data") or {}).get("passages"):
                return True
    return False


def _passage_material(result: dict) -> str:
    """
    Retrieved passages, laid out so the model can cite them.

    The citation marker sits immediately before each passage rather than in a
    separate list, because a model asked to match numbered sources to text it
    read earlier gets it wrong often enough to matter.
    """
    question = ""
    blocks = []
    for results in result.get("results", {}).values():
        for r in results:
            data = r.get("data") or {}
            question = question or data.get("question", "")
            for p in data.get("passages", []) or []:
                cite = p["doc"] + (f", p.{p['page']}" if p.get("page") else "")
                sect = f" — {p['section']}" if p.get("section") else ""
                blocks.append(f"[{cite}{sect}]\n{p['text']}")

    if not blocks:
        return ""
    head = f"Question: {question}\n\n" if question else ""
    return head + "Passages:\n\n" + "\n\n".join(blocks[:14])


def _fallback(result: dict) -> str:
    """Used when no model is reachable. Plain, and clearly so."""
    lines = list(result.get("summaries", []))
    if result.get("deliverables"):
        lines.append("")
        lines.append("Files produced:")
        lines += [f"  - {Path(d).name}" for d in result["deliverables"]]
    if not result.get("ok") and result.get("failed_step"):
        f = result["failed_step"]
        lines.append("")
        lines.append(f"Stopped at {f['step']} ({f['agent']}): "
                     f"{'; '.join(str(e) for e in f['errors'])}")
    return "\n".join(lines) or "No output produced."


# ------------------------------------------------------------------ compose

def compose(request: str,
            result: dict,
            model: str | None = None,
            session_id: str | None = None,
            history: list[str] | None = None,
            direct: bool = False) -> str:
    """Turn the run's output into the reply the user reads."""

    # No work was planned — this is conversation, not a task.
    if direct or not result.get("results"):
        try:
            answer = llm.converse(request, history, model=model,
                                  session_id=session_id)
            log_event("composer.done", session_id=session_id, mode="direct",
                      answer_chars=len(answer))
            return answer.strip() or "I'm here. What would you like to work on?"
        except Exception as e:
            log_event("composer.failed", session_id=session_id, error=str(e))
            return "I couldn't reach a model to answer that."

    has_deliverable = bool(result.get("deliverables"))

    # A run that was only the general agent is already the answer.
    if not has_deliverable:
        only = _only_general(result)
        if only:
            log_event("composer.passthrough", session_id=session_id,
                      agent="general", chars=len(only))
            return only

    written = _written_answer(result)

    if has_deliverable:
        mode = "report"
    elif written:
        # The rag agent already wrote the answer with the passages in front of
        # it. Regenerating here would only lose fidelity.
        mode = "trim"
    elif _has_passages(result):
        mode = "answer"
    else:
        mode = "content"

    if mode == "trim":
        material, dropped = written, []
    elif mode == "answer":
        material, dropped = _passage_material(result), []
    else:
        material, dropped = _material(result, full=(mode == "content"))

    if not material.strip():
        log_event("composer.no_material", session_id=session_id, dropped=dropped)
        if mode == "answer":
            return ("Nothing in the knowledge base matched that question. "
                    "Check the document is indexed on the Knowledge tab.")
        if dropped:
            return ("The model's output was unusable for this request — "
                    + "; ".join(dropped[:3])
                    + ". Try a larger model, or a clearer instruction.")
        return _fallback(result)

    system = {"report": SYSTEM_REPORT,
              "answer": SYSTEM_ANSWER,
              "trim": SYSTEM_TRIM}.get(mode, SYSTEM_CONTENT)

    if mode == "trim":
        prompt = (f"The user asked: {request}\n\nThe answer:\n{material}\n\n"
                  "Return it, tightened.")
    elif mode == "answer":
        prompt = f"{material}\n\nThe user asked: {request}\n\nAnswer it."
    else:
        prompt = f"The user asked:\n{request}\n\nWhat the agents produced:\n{material}"
    if has_deliverable:
        names = ", ".join(Path(d).name for d in result["deliverables"])
        prompt += f"\n\nFiles written: {names}"
    if dropped:
        prompt += ("\n\nSome agent output was discarded as unreliable: "
                   + "; ".join(dropped[:3]))
    if not result.get("ok") and result.get("failed_step"):
        prompt += f"\n\nA step failed: {json.dumps(result['failed_step'], default=str)}"
    prompt += "\n\nWrite the reply."

    try:
        answer = llm.chat(prompt, system=system, model=model,
                          capability="text", session_id=session_id,
                          max_tokens=2000, temperature=0.2)
    except Exception as e:
        log_event("composer.failed", session_id=session_id, error=str(e))
        return _fallback(result)

    # The composer itself can loop. Check its own output before shipping it.
    cleaned, why = _clean(answer)
    if not cleaned:
        log_event("composer.degenerate", session_id=session_id, reason=why)
        return written or _fallback(result)

    # Trimming should shorten, not gut. Losing more than half means the model
    # rewrote instead of editing, so the original stands.
    if mode == "trim" and len(cleaned) < len(material) * 0.5:
        log_event("composer.trim_rejected", session_id=session_id,
                  before=len(material), after=len(cleaned))
        return material

    log_event("composer.done", session_id=session_id, mode=mode,
              material_chars=len(material), answer_chars=len(cleaned),
              dropped=len(dropped))
    return cleaned.strip()
