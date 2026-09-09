"""
Document generation. Pure functions — the agent supplies structured
content, these turn it into files.

The separation matters: if the model wrote the document directly the
format would drift between runs and the organisation's house style could
not be enforced. The model produces fields; the template produces the
document.
"""

from __future__ import annotations

from datetime import date
from pathlib import Path

from ..audit import log_event


# ------------------------------------------------------------------ DOCX

def _add_sources(doc, sources: list[dict] | None):
    if not sources:
        return
    doc.add_heading("Sources", level=2)
    for s in sources:
        ref = s.get("doc") or s.get("source") or "unknown"
        page = s.get("page")
        line = f"{ref}" + (f", p.{page}" if page else "")
        doc.add_paragraph(line, style="List Bullet")


def write_approval_note(path: Path, content: dict) -> str:
    """
    The flagship deliverable. Fixed structure: reference number, subject,
    background, findings, recommendation, signature block.
    """
    from docx import Document
    from docx.shared import Pt
    from docx.enum.text import WD_ALIGN_PARAGRAPH

    doc = Document()

    head = doc.add_paragraph(content.get("organisation", "APPROVAL NOTE"))
    head.alignment = WD_ALIGN_PARAGRAPH.CENTER
    head.runs[0].bold = True
    head.runs[0].font.size = Pt(14)

    meta = doc.add_table(rows=0, cols=2)
    meta.style = "Table Grid"
    for label, key, default in (
        ("Reference No.", "ref_no", ""),
        ("Date", "date", date.today().isoformat()),
        ("Subject", "subject", ""),
        ("Prepared by", "prepared_by", ""),
    ):
        row = meta.add_row().cells
        row[0].text = label
        row[1].text = str(content.get(key, default) or "")

    doc.add_paragraph()

    for heading, key in (("Background", "background"),
                         ("Recommendation", "recommendation")):
        if content.get(key):
            doc.add_heading(heading, level=2)
            doc.add_paragraph(str(content[key]))

    findings = content.get("findings") or []
    if findings:
        doc.add_heading("Findings", level=2)
        t = doc.add_table(rows=1, cols=3)
        t.style = "Table Grid"
        hdr = t.rows[0].cells
        hdr[0].text, hdr[1].text, hdr[2].text = "#", "Finding", "Severity"
        for i, f in enumerate(findings, 1):
            c = t.add_row().cells
            c[0].text = str(i)
            if isinstance(f, dict):
                c[1].text = str(f.get("finding") or f.get("text") or "")
                c[2].text = str(f.get("severity", ""))
            else:
                c[1].text = str(f)
        doc.add_paragraph()

    _add_sources(doc, content.get("sources"))

    doc.add_paragraph()
    doc.add_paragraph()
    sig = doc.add_table(rows=1, cols=2)
    sig.rows[0].cells[0].text = f"Prepared by:\n\n{content.get('prepared_by', '')}"
    sig.rows[0].cells[1].text = f"Approved by:\n\n{content.get('approver', '')}"

    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    log_event("tool.docx", path=str(path), template="approval_note")
    return str(path)


def write_generic_docx(path: Path, content: dict) -> str:
    """Title + optional subtitle + sections [{heading, body|bullets}]."""
    from docx import Document

    doc = Document()
    doc.add_heading(str(content.get("title", "Document")), level=0)
    if content.get("subtitle"):
        doc.add_paragraph(str(content["subtitle"]))

    for sec in content.get("sections", []):
        if sec.get("heading"):
            doc.add_heading(str(sec["heading"]), level=1)
        if sec.get("body"):
            for para in str(sec["body"]).split("\n\n"):
                if para.strip():
                    doc.add_paragraph(para.strip())
        for b in sec.get("bullets", []) or []:
            doc.add_paragraph(str(b), style="List Bullet")
        for row_set in sec.get("tables", []) or []:
            if not row_set:
                continue
            t = doc.add_table(rows=1, cols=len(row_set[0]))
            t.style = "Table Grid"
            for j, h in enumerate(row_set[0]):
                t.rows[0].cells[j].text = str(h)
            for r in row_set[1:]:
                cells = t.add_row().cells
                for j, v in enumerate(r):
                    cells[j].text = str(v)
            doc.add_paragraph()

    _add_sources(doc, content.get("sources"))
    path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(path))
    log_event("tool.docx", path=str(path), template="generic")
    return str(path)


DOCX_TEMPLATES = {
    "approval_note": write_approval_note,
    "generic": write_generic_docx,
    "memo": write_generic_docx,
    "inspection_summary": write_generic_docx,
}


def write_docx(path: Path, content: dict, template: str = "generic") -> str:
    fn = DOCX_TEMPLATES.get(template, write_generic_docx)
    return fn(path, content)


# ------------------------------------------------------------------ PPTX

# The deck template is fixed. Every position, size, weight and colour below is
# a constant — the model supplies text and nothing else. Two reasons: a model
# asked to make styling decisions makes different ones each run, so no two
# decks match; and python-pptx's stock layouts produce Office-1997 output with
# a giant title placeholder and bullet dots.
#
# When a slide has more bullets than fit, the content splits onto a
# continuation slide rather than shrinking the type. Shrinking to fit is how
# decks end up with 11pt body text nobody can read from the back of a room.

SLIDE_W = 13.333          # inches, 16:9
SLIDE_H = 7.5

MARGIN_X = 0.85           # left and right text margin
CONTENT_W = SLIDE_W - 2 * MARGIN_X

TITLE_Y = 2.55            # title slide
TITLE_RULE_W = 1.6
TITLE_SIZE = 40
SUBTITLE_Y = 4.35
SUBTITLE_SIZE = 17

HEAD_Y = 0.55             # content slides
HEAD_H = 0.9
HEAD_SIZE = 26
RULE_Y = 1.45
BODY_Y = 1.85
BODY_H = 4.6
BODY_SIZE = 18            # fixed, never scaled
BODY_SPACE_AFTER = 12
BODY_LINE_SPACING = 1.3

PAGENO_SIZE = 11
SOURCE_SIZE = 14

MAX_BULLETS = 6           # beyond this, continue on the next slide
MAX_BULLET_CHARS = 130    # a bullet longer than this is a paragraph

FONT = "Calibri"          # present on every Windows and Office install

INK = (0x11, 0x18, 0x27)
MUTED = (0x64, 0x74, 0x8B)
ACCENT = (0x0F, 0x76, 0x6E)
RULE = (0xD5, 0xDB, 0xE3)


def _rgb(t):
    from pptx.dml.color import RGBColor
    return RGBColor(*t)


def _blank(prs):
    """Layout 6 is blank in the default template — no placeholders to fight."""
    return prs.slides.add_slide(prs.slide_layouts[6])


def _text(slide, x, y, w, h, content, size, colour, bold=False,
          align=None, space_after=0, line_spacing=1.0):
    """One text box, one paragraph per line. All styling passed in explicitly."""
    from pptx.util import Inches, Pt

    box = slide.shapes.add_textbox(Inches(x), Inches(y), Inches(w), Inches(h))
    tf = box.text_frame
    tf.word_wrap = True

    lines = content if isinstance(content, list) else [content]
    for i, line in enumerate(lines):
        para = tf.paragraphs[0] if i == 0 else tf.add_paragraph()
        para.text = str(line)
        para.font.size = Pt(size)
        para.font.bold = bold
        para.font.color.rgb = _rgb(colour)
        para.font.name = FONT
        para.space_after = Pt(space_after)
        para.line_spacing = line_spacing
        if align is not None:
            para.alignment = align
    return tf


def _rule(slide, x, y, w, colour, weight=1.0):
    from pptx.util import Inches, Pt
    from pptx.enum.shapes import MSO_SHAPE

    shp = slide.shapes.add_shape(MSO_SHAPE.RECTANGLE, Inches(x), Inches(y),
                                 Inches(w), Pt(weight))
    shp.fill.solid()
    shp.fill.fore_color.rgb = _rgb(colour)
    shp.line.fill.background()
    shp.shadow.inherit = False
    return shp


def _paginate(bullets: list[str]) -> list[list[str]]:
    """
    Split bullets across slides so type size never has to change.

    Long bullets count double — six one-liners fit the body box, but six
    wrapped paragraphs do not.
    """
    pages, current, weight = [], [], 0
    for b in bullets:
        cost = 2 if len(b) > MAX_BULLET_CHARS else 1
        if current and weight + cost > MAX_BULLETS:
            pages.append(current)
            current, weight = [], 0
        current.append(b)
        weight += cost
    if current:
        pages.append(current)
    return pages or [[]]


def _content_slide(prs, heading: str, bullets: list[str], page_no: int,
                   notes: str | None = None):
    from pptx.enum.text import PP_ALIGN

    s = _blank(prs)

    _text(s, MARGIN_X, HEAD_Y, CONTENT_W, HEAD_H,
          heading, HEAD_SIZE, INK, bold=True)
    _rule(s, MARGIN_X, RULE_Y, CONTENT_W, RULE, weight=1)

    if bullets:
        _text(s, MARGIN_X, BODY_Y, CONTENT_W, BODY_H,
              bullets, BODY_SIZE, INK,
              space_after=BODY_SPACE_AFTER,
              line_spacing=BODY_LINE_SPACING)

    _text(s, SLIDE_W - 1.5, 6.75, 0.9, 0.4,
          str(page_no), PAGENO_SIZE, MUTED, align=PP_ALIGN.RIGHT)

    if notes:
        s.notes_slide.notes_text_frame.text = str(notes)
    return s


def write_pptx(path: Path, content: dict, template: str = "generic") -> str:
    """
    content = {title, subtitle, slides:[{title, bullets[], notes}]}

    Only text comes from the caller. Layout, type and colour are fixed above.
    """
    from pptx import Presentation
    from pptx.util import Inches

    prs = Presentation()
    prs.slide_width = Inches(SLIDE_W)
    prs.slide_height = Inches(SLIDE_H)

    # ---------------------------------------------------------- title slide
    s = _blank(prs)
    _rule(s, MARGIN_X, TITLE_Y, TITLE_RULE_W, ACCENT, weight=3)
    _text(s, MARGIN_X, TITLE_Y + 0.2, CONTENT_W, 1.6,
          str(content.get("title", "Presentation")), TITLE_SIZE, INK, bold=True)
    if content.get("subtitle"):
        _text(s, MARGIN_X, SUBTITLE_Y, CONTENT_W, 0.8,
              str(content["subtitle"]), SUBTITLE_SIZE, MUTED)

    # -------------------------------------------------------- content slides
    page = 0
    total_slides = 0
    for slide in content.get("slides", []) or []:
        heading = str(slide.get("title", "")).strip()
        bullets = [str(b).strip() for b in (slide.get("bullets") or [])
                   if str(b).strip()]

        for i, chunk in enumerate(_paginate(bullets)):
            page += 1
            total_slides += 1
            head = heading if i == 0 else f"{heading} (cont.)"
            _content_slide(prs, head, chunk, page,
                           slide.get("notes") if i == 0 else None)

    # -------------------------------------------------------- sources slide
    srcs = content.get("sources") or []
    if srcs:
        seen, lines = set(), []
        for src in srcs:
            ref = src.get("doc") or src.get("source") or "unknown"
            pg = src.get("page")
            line = ref + (f", p.{pg}" if pg else "")
            if line not in seen:
                seen.add(line)
                lines.append(line)

        s = _blank(prs)
        _text(s, MARGIN_X, HEAD_Y, CONTENT_W, HEAD_H,
              "Sources", HEAD_SIZE, INK, bold=True)
        _rule(s, MARGIN_X, RULE_Y, CONTENT_W, RULE, weight=1)
        _text(s, MARGIN_X, BODY_Y, CONTENT_W, BODY_H,
              lines[:16], SOURCE_SIZE, MUTED, space_after=8)

    path.parent.mkdir(parents=True, exist_ok=True)
    prs.save(str(path))
    log_event("tool.pptx", path=str(path), n_slides=total_slides)
    return str(path)


# ------------------------------------------------------------------ XLSX

def write_xlsx(path: Path, content: dict) -> str:
    """
    content = {sheets:[{name, headers[], rows[[]], formulas[{cell, formula}]}]}
    """
    from openpyxl import Workbook
    from openpyxl.styles import Font, PatternFill
    from openpyxl.utils import get_column_letter

    wb = Workbook()
    wb.remove(wb.active)

    sheets = content.get("sheets") or [content]
    for spec in sheets:
        ws = wb.create_sheet(title=str(spec.get("name", "Sheet1"))[:31])

        headers = spec.get("headers") or []
        if headers:
            ws.append([str(h) for h in headers])
            fill = PatternFill("solid", fgColor="DDDDDD")
            for c in ws[1]:
                c.font = Font(bold=True)
                c.fill = fill

        for row in spec.get("rows", []) or []:
            ws.append(list(row))

        for f in spec.get("formulas", []) or []:
            ws[f["cell"]] = f["formula"]

        # Width by longest value, capped so one long cell doesn't blow it out.
        for j, col in enumerate(ws.columns, 1):
            width = max((len(str(c.value)) for c in col if c.value is not None),
                        default=10)
            ws.column_dimensions[get_column_letter(j)].width = min(max(width + 2, 10), 50)

    path.parent.mkdir(parents=True, exist_ok=True)
    wb.save(str(path))
    log_event("tool.xlsx", path=str(path), n_sheets=len(sheets))
    return str(path)
