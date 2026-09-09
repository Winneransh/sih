"""
Deliverable agents: DOCX, PPTX, XLSX.

Each runs a text model that produces *structured content*, then calls a
function to render it. The model never writes the document directly — if it
did, the format would drift between runs and the organisation's house style
could not be enforced.

The prompts are written to prevent padding. Left to itself a model asked for
a document will invent a background section, a risk assessment and a
conclusion whether or not the source material supports any of it. In an
approval note that is not verbose writing, it is fabricated content with a
signature block under it. So every prompt here says the same thing in the
same way: use the material, omit what is not there, add nothing.

Citations arrive from earlier steps and pass through untouched.
"""

from __future__ import annotations

import json
from datetime import date

from .. import llm
from ..tools import docgen
from ..tools.files import outputs_dir
from .base import Agent, AgentInput, AgentResult

# Grounding is conditional on where the material came from.
#
# Material extracted from the organisation's own documents must not be added
# to — a fabricated finding under a signature block is the failure this
# system exists to avoid. Material the general agent wrote is already model
# knowledge, and refusing to structure it would leave the document empty.
GROUNDING_SOURCED = (
    "Use ONLY the material given. Do not add background, context, "
    "recommendations, risks or conclusions that are not in it. If a field has "
    "no supporting material, leave it empty rather than filling it. Writing "
    "less is correct; inventing content is not."
)

GROUNDING_GENERATED = (
    "Structure the material given. Keep its substance — do not drop points or "
    "replace them with generalities. You may reword for the format, but do "
    "not introduce figures, dates or claims that are not in it."
)


def _grounding(inp: AgentInput) -> str:
    """Sourced material binds tightly; generated material binds loosely."""
    payload = inp.payload
    candidates = [payload] + list(payload.get("inputs") or [])
    for c in candidates:
        if isinstance(c, dict):
            data = c.get("data") if isinstance(c.get("data"), dict) else c
            if isinstance(data, dict) and data.get("sourced") is False:
                return GROUNDING_GENERATED
    return GROUNDING_SOURCED

APPROVAL_SCHEMA = {
    "type": "object",
    "properties": {
        "ref_no": {"type": "string"},
        "subject": {"type": "string"},
        "background": {"type": "string"},
        "findings": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "finding": {"type": "string"},
                    "severity": {"type": "string"},
                },
                "required": ["finding", "severity"],
            },
        },
        "recommendation": {"type": "string"},
    },
    "required": ["ref_no", "subject", "background", "findings", "recommendation"],
}

GENERIC_DOC_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "sections": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "heading": {"type": "string"},
                    "body": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                },
                "required": ["heading", "body"],
            },
        },
    },
    "required": ["title", "sections"],
}

DECK_SCHEMA = {
    "type": "object",
    "properties": {
        "title": {"type": "string"},
        "subtitle": {"type": "string"},
        "slides": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "title": {"type": "string"},
                    "bullets": {"type": "array", "items": {"type": "string"}},
                    "notes": {"type": "string"},
                },
                "required": ["title", "bullets"],
            },
        },
    },
    "required": ["title", "slides"],
}

SHEET_SCHEMA = {
    "type": "object",
    "properties": {
        "sheets": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "headers": {"type": "array", "items": {"type": "string"}},
                    "rows": {
                        "type": "array",
                        "items": {"type": "array", "items": {"type": "string"}},
                    },
                },
                "required": ["name", "headers", "rows"],
            },
        },
    },
    "required": ["sheets"],
}


def _input_material(inp: AgentInput, limit: int = 8000) -> str:
    """Flatten prior step results into the material the document is built from."""
    parts = []
    req = inp.context.get("request")
    if req:
        parts.append(f"Original request: {req}")

    for s in inp.context.get("prior_summaries", [])[-8:]:
        parts.append(f"- {s}")

    payload = inp.payload.get("inputs") or inp.payload.get("prev") or inp.payload

    # Written content reads better as prose than as a JSON blob, and a model
    # asked to structure JSON tends to preserve its keys in the output.
    written = _written_content(payload)
    if written:
        parts.append("Content:\n" + written[:limit])
    elif payload:
        try:
            parts.append("Extracted content:\n"
                         + json.dumps(payload, default=str)[:limit])
        except Exception:
            parts.append(str(payload)[:limit])
    return "\n".join(parts)[:limit]


def _written_content(payload) -> str:
    """Prose an earlier step wrote, pulled out of the payload wrapper."""
    if isinstance(payload, dict):
        data = payload.get("data") if isinstance(payload.get("data"), dict) else payload
        if isinstance(data, dict) and data.get("content"):
            return str(data["content"])
    if isinstance(payload, list):
        chunks = [_written_content(p) for p in payload]
        chunks = [c for c in chunks if c]
        if chunks:
            return "\n\n".join(chunks)
    return ""


def _collect_sources(inp: AgentInput) -> list[dict]:
    return inp.context.get("sources", []) or inp.payload.get("sources", []) or []


def _strip_empty(content: dict) -> dict:
    """Drop empty sections so the template does not render blank headings."""
    if "sections" in content:
        content["sections"] = [
            s for s in content["sections"]
            if (s.get("body") or "").strip() or s.get("bullets")
        ]
    if "slides" in content:
        content["slides"] = [
            s for s in content["slides"]
            if [b for b in (s.get("bullets") or []) if str(b).strip()]
        ]
    return content


class DocxAgent(Agent):
    name = "docx"
    capability = "text"
    description = (
        "Produces Word documents from content gathered by earlier steps. "
        "Templates: approval_note (reference no, subject, background, findings "
        "table, recommendation, signature block), inspection_summary, memo, "
        "generic. Carries citations through into the file."
    )
    input_kind = "structured content from prior steps"
    output_kind = ".docx file"

    def execute(self, inp: AgentInput) -> AgentResult:
        template = inp.options.get("template", "generic")
        material = _input_material(inp)

        if template == "approval_note":
            schema = APPROVAL_SCHEMA
            guide = (
                "Draft an approval note from the material below.\n\n"
                "Every finding must appear in the material. If the material "
                "contains no findings, return an empty findings list. Keep "
                "background to what the material states — one or two sentences "
                "is normal. The recommendation must follow from the findings; "
                "if none support a recommendation, leave it empty."
            )
        else:
            schema = GENERIC_DOC_SCHEMA
            guide = (
                "Write a document from the material below.\n\n"
                "One section per topic actually present in the material. Do "
                "not add an introduction, overview or conclusion unless the "
                "material contains one. Two well-grounded sections are better "
                "than six padded ones."
            )

        prompt = (f"{guide}\n\n{_grounding(inp)}\n\nTask: {inp.task}\n\n"
                  f"Material:\n{material}\n\nReturn JSON only.")
        content = llm.chat_json(prompt, schema, model=inp.model,
                                capability="text", session_id=inp.session_id,
                                max_tokens=3000, temperature=0.2)
        content = _strip_empty(content)

        if template == "approval_note":
            content.setdefault("date", date.today().isoformat())
            content.setdefault("organisation", inp.options.get(
                "organisation", "APPROVAL NOTE"))
            content.setdefault("prepared_by", inp.options.get("prepared_by", ""))
            content.setdefault("approver", inp.options.get("approver", ""))

        content["sources"] = _collect_sources(inp)

        fname = inp.options.get("filename") or f"{template}_{date.today().isoformat()}.docx"
        path = outputs_dir(inp.session_id) / fname
        docgen.write_docx(path, content, template)

        return AgentResult(
            agent=self.name, ok=True,
            summary=f"Wrote {template} -> {path.name}",
            data={"template": template,
                  "title": content.get("subject") or content.get("title"),
                  "n_findings": len(content.get("findings", [])),
                  "n_sections": len(content.get("sections", []))},
            artifacts=[str(path)],
            sources=content["sources"],
            model_used=inp.model,
        )


class PptxAgent(Agent):
    name = "pptx"
    capability = "text"
    description = (
        "Produces PowerPoint decks (board presentations, review packs) from "
        "content gathered by earlier steps. Adds a sources slide when "
        "citations are present."
    )
    input_kind = "structured content from prior steps"
    output_kind = ".pptx file"

    def execute(self, inp: AgentInput) -> AgentResult:
        material = _input_material(inp)
        n = int(inp.options.get("max_slides", 8))
        prompt = (
            f"Build a presentation of at most {n} content slides.\n\n"
            "One slide per topic actually present in the material. Three to "
            "five bullets per slide, one line each, no full sentences. Fewer "
            "well-grounded slides are better than a padded deck — do not add "
            "an agenda, overview, 'next steps' or 'thank you' slide unless the "
            f"material contains that content.\n\n{_grounding(inp)}\n\n"
            f"Task: {inp.task}\n\nMaterial:\n{material}\n\nReturn JSON only."
        )
        content = llm.chat_json(prompt, DECK_SCHEMA, model=inp.model,
                                capability="text", session_id=inp.session_id,
                                max_tokens=3000, temperature=0.2)
        content = _strip_empty(content)
        content["sources"] = _collect_sources(inp)

        fname = inp.options.get("filename") or f"deck_{date.today().isoformat()}.pptx"
        path = outputs_dir(inp.session_id) / fname
        docgen.write_pptx(path, content)

        return AgentResult(
            agent=self.name, ok=True,
            summary=f"Wrote deck with {len(content.get('slides', []))} slides -> {path.name}",
            data={"title": content.get("title"),
                  "n_slides": len(content.get("slides", []))},
            artifacts=[str(path)],
            sources=content["sources"],
            model_used=inp.model,
        )


class XlsxAgent(Agent):
    name = "xlsx"
    capability = "text"
    description = (
        "Produces Excel workbooks from tabular data gathered by earlier steps — "
        "registers, comparisons, extracted tables. One sheet per logical table."
    )
    input_kind = "tabular data from prior steps"
    output_kind = ".xlsx file"

    def execute(self, inp: AgentInput) -> AgentResult:
        material = _input_material(inp, limit=10000)
        prompt = (
            "Build a spreadsheet from the material below.\n\n"
            "Every row must come from the material. Do not add summary rows, "
            "totals or derived columns unless the material contains them. If "
            f"the material has no tabular data, return one empty sheet.\n\n"
            f"{_grounding(inp)}\n\nTask: {inp.task}\n\nMaterial:\n{material}\n\n"
            "Return JSON only."
        )
        content = llm.chat_json(prompt, SHEET_SCHEMA, model=inp.model,
                                capability="text", session_id=inp.session_id,
                                max_tokens=4000, temperature=0.2)

        fname = inp.options.get("filename") or f"data_{date.today().isoformat()}.xlsx"
        path = outputs_dir(inp.session_id) / fname
        docgen.write_xlsx(path, content)

        rows = sum(len(s.get("rows", [])) for s in content.get("sheets", []))
        return AgentResult(
            agent=self.name, ok=True,
            summary=f"Wrote {len(content.get('sheets', []))} sheet(s), {rows} rows -> {path.name}",
            data={"n_sheets": len(content.get("sheets", [])), "n_rows": rows},
            artifacts=[str(path)],
            sources=_collect_sources(inp),
            model_used=inp.model,
        )
