"""
General agent.

Everything else in the roster is grounded: the rag agent may only use
retrieved passages, the vision agent only what is in the image, the document
agents only material handed to them. That is correct for work about the
organisation's own documents, and useless for everything else. Asked to write
about a subject the organisation has never written down, the grounded agents
have nothing to work from and the document agents receive empty material.

This agent covers that. It answers and it writes, from the model's own
knowledge, on any subject.

It is a peer, not a helper for one other agent. Its output is text, and text
is what every other agent consumes:

    general                          -> answer a question
    general -> pptx                  -> a deck about a subject
    general -> docx                  -> a written document
    general -> xlsx                  -> a table of something
    general -> code                  -> a spec, then an implementation
    rag -> general                   -> explain retrieved material further
    vision -> general                -> reason about what was read

Its output is marked unsourced. Nothing downstream should present model
knowledge as though it came from the organisation's documents, and a deck
built from it should be distinguishable from one built from an inspection
report.
"""

from __future__ import annotations

from .. import llm
from ..textcheck import is_degenerate, strip_repeats
from .base import Agent, AgentInput, AgentResult

SYSTEM = """You answer questions and write content on any subject.

Write plainly and specifically. Concrete detail beats general statements.
Match the length to what was asked — a short question gets a short answer.

Say plainly when you are unsure of something rather than stating it
confidently. Do not invent figures, dates, names or citations. If a precise
number matters and you do not know it, say so instead of guessing.

No preamble, no restating the question, no closing offer of further help.
Start with the substance."""


class GeneralAgent(Agent):
    name = "general"
    capability = "text"
    description = (
        "Answers questions and writes content on any subject from the model's "
        "own knowledge — explanations, background, drafts, outlines, "
        "structured material. Use it when the answer is not in the "
        "organisation's documents and not in an attached file: general "
        "knowledge questions, or as the first step when a document, deck or "
        "spreadsheet is wanted about a subject with no source material. Its "
        "output feeds any other agent. Output is model knowledge, not sourced "
        "from the organisation's documents."
    )
    input_kind = "question, topic, or material from a prior step"
    output_kind = "written content"

    def execute(self, inp: AgentInput) -> AgentResult:
        task = (inp.task or inp.payload.get("question")
                or inp.payload.get("topic") or "")
        if not task:
            return AgentResult(self.name, False, "nothing to write about",
                               error="general agent needs a question or topic")

        prompt = self._prompt(task, inp)
        length = inp.options.get("length", "auto")
        max_tokens = {"short": 400, "auto": 1200, "long": 2500}.get(length, 1200)

        text = llm.chat(prompt, system=SYSTEM, model=inp.model,
                        capability="text", session_id=inp.session_id,
                        max_tokens=int(inp.options.get("max_tokens", max_tokens)),
                        temperature=float(inp.options.get("temperature", 0.5)))

        bad, why = is_degenerate(text, prompt)
        if bad:
            text = strip_repeats(text)
            still_bad, why2 = is_degenerate(text, prompt)
            if still_bad:
                return AgentResult(
                    self.name, False,
                    "the model produced unusable output",
                    data={"reason": why2 or why}, error=why2 or why)

        text = text.strip()

        # Long output goes to disk so a downstream agent reads it from a path
        # rather than carrying it through the planner's context.
        artifacts = []
        if len(text) > 3000:
            artifacts.append(self.spill(
                inp.session_id, f"general_{abs(hash(text)) % 99999}.txt", text))

        return AgentResult(
            agent=self.name, ok=True,
            summary=text if len(text) <= 1200 else text[:1200] + " …",
            data={
                "content": text,
                "topic": task[:200],
                # Downstream agents and the composer read this. Model knowledge
                # must not be presented as if it came from a source document.
                "sourced": False,
            },
            artifacts=artifacts,
            sources=[],
            model_used=inp.model,
        )

    def _prompt(self, task: str, inp: AgentInput) -> str:
        """
        Prior steps are context, not constraint. Unlike the grounded agents,
        this one may go beyond what it was handed — that is the point of it.
        """
        parts = []

        prior = inp.context.get("prior_summaries") or []
        if prior:
            parts.append("Earlier in this task:\n"
                         + "\n".join(f"- {p}" for p in prior[-4:]))

        payload = inp.payload.get("inputs") or inp.payload.get("prev")
        if payload:
            import json
            try:
                blob = json.dumps(payload, default=str)[:4000]
            except Exception:
                blob = str(payload)[:4000]
            parts.append(f"Material from earlier steps:\n{blob}")

        # A downstream document agent needs material it can structure, not
        # prose it has to take apart again.
        fmt = inp.options.get("format")
        if fmt == "outline":
            parts.append("Write it as a structured outline: short headings, "
                         "each with three to five specific points.")
        elif fmt == "sections":
            parts.append("Write it as sections, each with a heading and a "
                         "short body.")
        elif fmt == "table":
            parts.append("Write it as a table: a header row, then rows of "
                         "values. Plain text, one row per line.")

        parts.append(task)
        return "\n\n".join(parts)
