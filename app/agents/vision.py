"""
Vision Agent.

One agent, one vision model, one job: read the image and report what is
actually in it. There are no separate modes for transcription, tables,
drawings or photographs — splitting the work that way meant the planner had
to guess the mode in advance, and it guessed wrong. Asked to "describe this
drawing" it would pick transcription and return three words of text.

So the agent does the whole job in one pass: transcribe the text, extract any
tables, capture tags and title blocks if it is a drawing, and describe what
the image shows. The caller decides which parts of that it cares about.

Prompts are deliberately terse. A small vision model given several sentences
of instruction plus a restated task echoes the instructions back and loops
until it hits the token cap — verbose prompts make the output worse, not
better.
"""

from __future__ import annotations

from pathlib import Path

from .. import llm
from ..textcheck import is_degenerate, strip_repeats
from ..tools.intake import tile_image
from .base import Agent, AgentInput, AgentResult

# One pass, everything the image contains. Sections are optional — the model
# omits what does not apply rather than inventing a placeholder.
READ_PROMPT = """Read this image and report only what is actually visible.

TEXT: transcribe all text exactly as written, preserving line order.
TABLES: any tabular data, row by row.
LABELS: any tags, numbers, codes, or title block fields.
DESCRIPTION: what the image shows.

Omit a section entirely if it does not apply. Never guess at anything
unclear — write [unclear] instead. Add nothing that is not in the image."""

# Kept only where the caller genuinely needs machine-readable output.
STRUCTURED_PROMPT = """Extract from this image as JSON:
{"report_ref":"","date":"","equipment":"","findings":[{"finding":"","severity":"high|medium|low","location":"","action":""}],"tables":[{"headers":[],"rows":[[]]}],"tags":[],"title_block":{"drawing_no":"","revision":"","title":""}}

Use "" or [] for anything not present. Never invent a value."""


class VisionAgent(Agent):
    name = "vision"
    capability = "vision"
    description = (
        "Reads any image or scanned page in a single pass — printed text, "
        "handwriting, tables, engineering drawings, P&ID tags and title "
        "blocks, photographs of plant and equipment. Returns transcription, "
        "extracted tables, labels and a description together. Set "
        "options.structured=true when machine-readable fields are needed "
        "instead of prose. Tiles large drawings so small tag text stays "
        "readable."
    )
    input_kind = "images or page images"
    output_kind = "transcription, tables, labels and description"

    def execute(self, inp: AgentInput) -> AgentResult:
        images = self._gather_images(inp)
        if not images:
            return AgentResult(self.name, False, "no images supplied",
                               error="vision agent received no images")

        structured = bool(inp.options.get("structured"))
        prompt = self._build_prompt(structured, inp.task)

        # Dense drawings lose their tag text when the whole sheet is
        # downscaled to the model's input size, so read crops instead.
        if inp.options.get("tile") or self._is_large(images[0]):
            images = tile_image(inp.session_id, Path(images[0]),
                                cols=inp.options.get("tile_cols", 2),
                                rows=inp.options.get("tile_rows", 2))

        outputs, rejected = [], []
        for img in images:
            text = llm.vision(prompt, [img], model=inp.model,
                              session_id=inp.session_id,
                              max_tokens=int(inp.options.get("max_tokens", 1500)))

            bad, why = is_degenerate(text, prompt)
            if bad:
                salvaged = strip_repeats(text)
                still_bad, _ = is_degenerate(salvaged, prompt)
                if still_bad:
                    rejected.append(f"{Path(img).name}: {why}")
                    continue
                text = salvaged
            outputs.append(text)

        if not outputs:
            return AgentResult(
                self.name, False,
                f"the vision model produced unusable output for all "
                f"{len(images)} image(s)",
                data={"rejected": rejected},
                error="; ".join(rejected[:3]) or "degenerate model output")

        combined = "\n\n".join(outputs)

        if structured:
            data = self._merge_json(outputs)
            summary = self._summarise_structured(data, len(outputs))
        else:
            data = {"content": combined, "n_images": len(outputs)}
            summary = self._preview(combined, len(outputs))

        if rejected:
            summary += f" ({len(rejected)} image(s) unreadable)"

        # Full output always goes to disk; only the summary travels onward.
        path = self.spill(inp.session_id,
                          f"vision_{abs(hash(combined)) % 99999}.txt", combined)

        return AgentResult(
            agent=self.name, ok=True, summary=summary,
            data=data, artifacts=[path], model_used=inp.model,
            sources=[{"doc": Path(images[0]).name, "page": None}],
        )

    # ---------------------------------------------------------------- utils

    def _build_prompt(self, structured: bool, task: str | None) -> str:
        """
        The planner's task text usually restates what the agent already does.
        Appending it verbatim gives the model two overlapping instruction
        blocks, which is what makes small models echo and loop. Append only
        when the task adds a genuine constraint.
        """
        base = STRUCTURED_PROMPT if structured else READ_PROMPT
        t = (task or "").strip()
        if not t or len(t) > 160:
            return base

        boilerplate = ("extract", "transcribe", "analyze", "analyse", "read",
                       "describe", "do not", "return json", "structured")
        if any(t.lower().startswith(b) for b in boilerplate):
            return base
        return f"{base}\n\nAlso answer: {t}"

    def _preview(self, text: str, n: int) -> str:
        head = " ".join(text.split())[:260]
        return f"Read {n} image(s). {head}{'…' if len(text) > 260 else ''}"

    def _gather_images(self, inp: AgentInput) -> list[str]:
        p = inp.payload
        for key in ("images", "page_images", "files"):
            v = p.get(key)
            if v:
                return [str(x) for x in (v if isinstance(v, list) else [v])]
        if p.get("path"):
            return [str(p["path"])]
        # A follow-up question may arrive with prior-step payloads rather than
        # files; recover the images from those instead of failing.
        for item in (p.get("inputs") or []):
            if isinstance(item, dict):
                for key in ("images", "page_images"):
                    if item.get(key):
                        return [str(x) for x in item[key]]
        return []

    def _is_large(self, path: str) -> bool:
        try:
            from PIL import Image
            with Image.open(path) as im:
                return im.size[0] * im.size[1] > 4_000_000
        except Exception:
            return False

    def _merge_json(self, outputs: list[str]) -> dict:
        merged: dict = {}
        for o in outputs:
            try:
                d = llm.extract_json(o)
            except ValueError:
                continue
            if not isinstance(d, dict):
                continue
            for k, v in d.items():
                if isinstance(v, list):
                    merged.setdefault(k, []).extend(v)
                elif k not in merged or not merged[k]:
                    merged[k] = v
        if not merged:
            merged = {"raw": "\n\n".join(outputs)[:3000], "parse_failed": True}
        return merged

    def _summarise_structured(self, data: dict, n: int) -> str:
        if data.get("parse_failed"):
            return f"Read {n} image(s); the model did not return valid JSON."
        bits = []
        if data.get("findings"):
            highs = sum(1 for x in data["findings"]
                        if str(x.get("severity", "")).lower() == "high")
            bits.append(f"{len(data['findings'])} finding(s), {highs} high severity")
        if data.get("tables"):
            rows = sum(len(t.get("rows", [])) for t in data["tables"])
            bits.append(f"{len(data['tables'])} table(s), {rows} rows")
        if data.get("tags"):
            bits.append(f"{len(data['tags'])} tag(s)")
        tb = data.get("title_block") or {}
        if tb.get("drawing_no"):
            bits.append(f"drawing {tb['drawing_no']} rev {tb.get('revision') or '?'}")
        return f"Read {n} image(s): " + ("; ".join(bits) if bits else "no structured fields found")
