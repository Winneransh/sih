"""
Intake. Deterministic file handling — no model involved.

Works out what each attachment is (text-layer PDF, scanned PDF, image,
office file), renders pages to images where the Vision Agent will need
them, and produces the compact inventory the planner reads.

Asking a model whether a PDF has a text layer would be slower and less
reliable than checking.
"""

from __future__ import annotations

from pathlib import Path

from ..audit import log_event
from .files import session_dir, work_dir

IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".tif", ".gif"}
OFFICE_EXT = {".docx", ".xlsx", ".pptx", ".doc", ".xls", ".ppt"}
TEXT_EXT = {".txt", ".md", ".csv", ".json", ".log", ".py", ".yaml", ".yml"}

MIN_TEXT_CHARS_PER_PAGE = 60      # below this, treat the page as scanned


def _pdf_text_layer(path: Path) -> tuple[bool, int, str]:
    """Returns (has_text_layer, page_count, sample_text)."""
    try:
        import pypdf
    except ImportError:
        return False, 0, ""
    try:
        reader = pypdf.PdfReader(str(path))
        pages = len(reader.pages)
        sample = ""
        for pg in reader.pages[:3]:
            sample += (pg.extract_text() or "")
        has_text = len(sample.strip()) > MIN_TEXT_CHARS_PER_PAGE
        return has_text, pages, sample
    except Exception:
        return False, 0, ""


def render_pdf_pages(session_id: str, pdf: Path, dpi: int = 180,
                     max_pages: int = 40) -> list[Path]:
    """Rasterise PDF pages so a vision model can read them."""
    try:
        import pypdfium2 as pdfium
    except ImportError as e:
        raise RuntimeError(
            "pypdfium2 is required to render scanned PDFs — pip install pypdfium2"
        ) from e

    out_dir = work_dir(session_id) / f"{pdf.stem}_pages"
    out_dir.mkdir(parents=True, exist_ok=True)

    doc = pdfium.PdfDocument(str(pdf))
    scale = dpi / 72
    paths = []
    for i in range(min(len(doc), max_pages)):
        img = doc[i].render(scale=scale).to_pil()
        p = out_dir / f"page_{i + 1:03d}.png"
        img.save(p)
        paths.append(p)
    log_event("intake.render_pdf", session_id=session_id,
              pdf=str(pdf), pages=len(paths))
    return paths


def tile_image(session_id: str, image: Path, cols: int = 2, rows: int = 2,
               overlap: float = 0.08) -> list[Path]:
    """
    Split a large image into overlapping crops.

    Dense drawings (P&IDs especially) carry tag text that is unreadable
    when the whole sheet is downscaled to fit a vision model's input.
    Overlap prevents a tag being cut in half at a tile boundary.
    """
    from PIL import Image

    out_dir = work_dir(session_id) / f"{image.stem}_tiles"
    out_dir.mkdir(parents=True, exist_ok=True)

    im = Image.open(image)
    W, H = im.size
    tw, th = W / cols, H / rows
    ox, oy = tw * overlap, th * overlap

    paths = []
    for r in range(rows):
        for c in range(cols):
            box = (
                max(0, int(c * tw - ox)),
                max(0, int(r * th - oy)),
                min(W, int((c + 1) * tw + ox)),
                min(H, int((r + 1) * th + oy)),
            )
            p = out_dir / f"tile_r{r}c{c}.png"
            im.crop(box).save(p)
            paths.append(p)

    log_event("intake.tile_image", session_id=session_id,
              image=str(image), tiles=len(paths))
    return paths


def inspect(session_id: str, path: Path) -> dict:
    """Classify one file and prepare whatever downstream agents will need."""
    ext = path.suffix.lower()
    rec: dict = {
        "path": str(path.relative_to(session_dir(session_id))),
        "abs_path": str(path),
        "name": path.name,
        "ext": ext,
        "size": path.stat().st_size,
    }

    if ext == ".pdf":
        has_text, pages, sample = _pdf_text_layer(path)
        rec["pages"] = pages
        if has_text:
            rec["kind"] = "pdf_text"
            rec["text_preview"] = sample[:500]
        else:
            rec["kind"] = "pdf_scanned"
            rec["page_images"] = [str(p) for p in render_pdf_pages(session_id, path)]

    elif ext in IMAGE_EXT:
        rec["kind"] = "image"
        try:
            from PIL import Image
            with Image.open(path) as im:
                rec["width"], rec["height"] = im.size
                # A large, wide sheet is almost certainly a drawing, not a photo.
                rec["large"] = (im.size[0] * im.size[1]) > 4_000_000
        except Exception:
            pass

    elif ext in OFFICE_EXT:
        rec["kind"] = "office"

    elif ext in TEXT_EXT:
        rec["kind"] = "text"
        rec["text_preview"] = path.read_text(
            encoding="utf-8", errors="replace")[:500]
    else:
        rec["kind"] = "binary"

    return rec


def build_inventory(session_id: str, paths: list[Path]) -> list[dict]:
    """The compact file summary handed to the planner. No file contents."""
    inv = [inspect(session_id, p) for p in paths]
    log_event("intake.inventory", session_id=session_id, n_files=len(inv),
              kinds=[i["kind"] for i in inv])
    return inv


def inventory_for_planner(inv: list[dict]) -> list[dict]:
    """Strip previews and image lists — the planner needs shape, not content."""
    out = []
    for i in inv:
        e = {"path": i["path"], "kind": i["kind"], "name": i["name"]}
        if "pages" in i:
            e["pages"] = i["pages"]
        if i.get("page_images"):
            e["n_page_images"] = len(i["page_images"])
        if i.get("large"):
            e["large_image"] = True
        out.append(e)
    return out
