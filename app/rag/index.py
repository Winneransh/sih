"""
The index. Chunking and embedding are deterministic — they always happen.

No model decides whether a document gets embedded. A PDF that enters the
system, by any route, is chunked and embedded on arrival. That is the whole
point: by the time anything is asked about a document, the document is
already searchable.

Every chunk carries its document name and page from the moment it is created.
That metadata travels through retrieval, through scoring, into the answer and
into any file generated from it. Nothing in the chain is allowed to drop it.

Storage is a JSON index, which is fine at the scale this runs at. Swap it for
a real vector store behind `search()` when the corpus grows — the signature
is the seam.
"""

from __future__ import annotations

import json
import math
import re
import threading
from collections import Counter
from pathlib import Path

from .. import llm
from ..audit import log_event
from ..config import KB_DIR

INDEX_PATH = KB_DIR / "index.json"
DOCS_DIR = KB_DIR / "docs"
DOCS_DIR.mkdir(parents=True, exist_ok=True)

CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150

# Numbered clauses, all-caps headings and markdown headings. Manuals and
# procedures are structured this way, and splitting on those boundaries keeps
# a procedure whole instead of cutting it mid-step.
SECTION_RE = re.compile(
    r"^\s*(?:\d+(?:\.\d+)*\s+\S.*|[A-Z][A-Z \-/&]{4,}|#{1,4}\s+.+)$", re.M)

_lock = threading.Lock()


# ------------------------------------------------------------------ storage

def _load() -> dict:
    if not INDEX_PATH.exists():
        return {"chunks": [], "dims": 0}
    return json.loads(INDEX_PATH.read_text())


def _save(idx: dict) -> None:
    INDEX_PATH.write_text(json.dumps(idx))


def _tokens(s: str) -> list[str]:
    return re.findall(r"[a-z0-9]+", s.lower())


# ----------------------------------------------------------------- chunking

def chunk_text(text: str, page: int | None = None) -> list[dict]:
    """Section-aware split, then packed to size with overlap."""
    positions = [m.start() for m in SECTION_RE.finditer(text)]
    blocks: list[tuple[str, str | None]] = []

    if positions:
        bounds = [0] + positions + [len(text)]
        for i in range(len(bounds) - 1):
            seg = text[bounds[i]:bounds[i + 1]].strip()
            if not seg:
                continue
            heading = seg.splitlines()[0].strip()[:120] if i > 0 else None
            blocks.append((seg, heading))
    else:
        blocks = [(text, None)]

    out = []
    for seg, heading in blocks:
        if len(seg) <= CHUNK_CHARS:
            pieces = [seg]
        else:
            pieces, start = [], 0
            while start < len(seg):
                pieces.append(seg[start:start + CHUNK_CHARS])
                start += CHUNK_CHARS - CHUNK_OVERLAP
        for p in pieces:
            if p.strip():
                out.append({"text": p.strip(), "page": page, "section": heading})
    return out


# ---------------------------------------------------------------- ingestion

def chunk_and_embed(doc: str, text: str, page: int | None = None,
                    source_path: str | None = None,
                    session_id: str | None = None) -> int:
    """
    The one entry point. Chunk, embed, store. Always.

    Returns the number of chunks added.
    """
    chunks = chunk_text(text, page)
    if not chunks:
        return 0

    vectors = llm.embed([c["text"] for c in chunks], session_id=session_id)

    with _lock:
        idx = _load()
        base = len(idx["chunks"])
        for i, (c, v) in enumerate(zip(chunks, vectors)):
            c.update({
                "id": base + i,
                "doc": doc,
                "vector": v,
                "source_path": source_path,
                "tf": dict(Counter(_tokens(c["text"]))),
            })
            idx["chunks"].append(c)
        idx["dims"] = len(vectors[0])
        _save(idx)

    log_event("rag.ingest", doc=doc, page=page, chunks=len(chunks),
              session_id=session_id)
    return len(chunks)


def ingest_text(doc: str, text: str, **kw) -> int:
    return chunk_and_embed(doc, text, **kw)


def ingest_file(path: Path, doc: str | None = None,
                session_id: str | None = None) -> int:
    """
    Any supported file, one page at a time where pages exist.

    A PDF's own text layer is read directly. A PDF without one is rendered
    and read by the vision model first — the caller does that and passes the
    result back here as text, because this module never decides what a
    document is.
    """
    doc = doc or path.name
    total = 0

    if path.suffix.lower() == ".pdf":
        import pypdf
        reader = pypdf.PdfReader(str(path))
        for i, pg in enumerate(reader.pages, 1):
            t = (pg.extract_text() or "").strip()
            if t:
                total += chunk_and_embed(doc, t, page=i,
                                         source_path=str(path),
                                         session_id=session_id)
    else:
        total = chunk_and_embed(
            doc, path.read_text(encoding="utf-8", errors="replace"),
            source_path=str(path), session_id=session_id)
    return total


# ---------------------------------------------------------------- retrieval

def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a))
    nb = math.sqrt(sum(y * y for y in b))
    return dot / (na * nb) if na and nb else 0.0


def _bm25(query_tokens: list[str], chunks: list[dict],
          k1: float = 1.5, b: float = 0.75) -> list[float]:
    """
    Keyword scoring alongside the vectors.

    Documents here are full of tag numbers, equipment codes and clause
    references. Embeddings handle those badly — an exact-token match is often
    the strongest signal available.
    """
    N = len(chunks)
    if N == 0:
        return []
    lens = [sum(c["tf"].values()) for c in chunks]
    avg = sum(lens) / N

    df = Counter()
    for c in chunks:
        for t in set(query_tokens):
            if t in c["tf"]:
                df[t] += 1

    scores = []
    for c, L in zip(chunks, lens):
        s = 0.0
        for t in query_tokens:
            f = c["tf"].get(t, 0)
            if not f:
                continue
            idf = math.log(1 + (N - df[t] + 0.5) / (df[t] + 0.5))
            s += idf * (f * (k1 + 1)) / (f + k1 * (1 - b + b * L / avg))
        scores.append(s)
    return scores


def search(query: str, top_k: int = 5, docs: list[str] | None = None,
           session_id: str | None = None) -> list[dict]:
    """
    Hybrid dense + keyword, fused by reciprocal rank.

    `docs` scopes the search to named documents. When the resolver has already
    established which document the question is about, searching the rest of
    the corpus only adds noise.
    """
    idx = _load()
    chunks = idx["chunks"]

    if docs:
        wanted = {d.lower() for d in docs}
        chunks = [c for c in chunks if c["doc"].lower() in wanted]
    if not chunks:
        return []

    qvec = llm.embed([query], session_id=session_id)[0]
    dense = [_cosine(qvec, c["vector"]) for c in chunks]
    sparse = _bm25(_tokens(query), chunks)

    dense_rank = {i: r for r, i in enumerate(
        sorted(range(len(dense)), key=lambda i: -dense[i]))}
    sparse_rank = {i: r for r, i in enumerate(
        sorted(range(len(sparse)), key=lambda i: -sparse[i]))}

    K = 60
    fused = sorted(
        ((i, 1 / (K + dense_rank[i]) + 1 / (K + sparse_rank[i]))
         for i in range(len(chunks))),
        key=lambda t: -t[1])

    hits = [{
        "text": chunks[i]["text"],
        "doc": chunks[i]["doc"],
        "page": chunks[i]["page"],
        "section": chunks[i].get("section"),
        "score": round(s, 5),
    } for i, s in fused[:top_k]]

    log_event("rag.search", query=query[:120], n_hits=len(hits),
              scoped_to=docs, session_id=session_id)
    return hits


# --------------------------------------------------------------- inspection

def documents() -> list[dict]:
    """What is indexed. The resolver matches against this."""
    idx = _load()
    by_doc: dict[str, dict] = {}
    for c in idx["chunks"]:
        d = by_doc.setdefault(c["doc"], {"doc": c["doc"], "chunks": 0,
                                         "pages": set(),
                                         "source_path": c.get("source_path")})
        d["chunks"] += 1
        if c.get("page"):
            d["pages"].add(c["page"])
    out = []
    for d in by_doc.values():
        out.append({"doc": d["doc"], "chunks": d["chunks"],
                    "pages": len(d["pages"]), "source_path": d["source_path"]})
    return sorted(out, key=lambda d: d["doc"].lower())


def stats() -> dict:
    idx = _load()
    docs = documents()
    return {"n_chunks": len(idx["chunks"]), "n_docs": len(docs),
            "docs": [d["doc"] for d in docs], "dims": idx.get("dims", 0)}


def clear() -> None:
    with _lock:
        if INDEX_PATH.exists():
            INDEX_PATH.unlink()
    log_event("rag.clear")
