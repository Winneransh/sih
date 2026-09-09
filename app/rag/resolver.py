"""
Document resolver. Deterministic. Runs before the planner.

The planner used to be left to work out whether a document the user named
actually existed, which it cannot do — it has never seen the index. So it
guessed, and a question about a text PDF went to the vision model.

This module answers that question in code. It scans the message for anything
that looks like a document reference, matches it against what is attached and
what is indexed, and hands the planner a verdict. The planner is told, not
asked.

Nothing here is hardcoded to any document. `.pdf` is a file extension, not a
name; every other candidate is matched against whatever happens to be in the
index at the time.
"""

from __future__ import annotations

import re

from ..audit import log_event
from .index import documents

# A filename with an extension. The extension list is the only fixed thing
# here — these are formats, not documents.
#
# No spaces: allowing them makes the match greedy and swallows the words
# before the filename. A name that genuinely contains spaces is caught by the
# quoted-string pattern instead.
FILENAME_RE = re.compile(
    r"\b[\w][\w\-.]{0,80}\.(?:pdf|docx?|xlsx?|pptx?|txt|md|csv)\b", re.I)

# Quoted strings, and tokens that look like an identifier rather than an
# ordinary word — uppercase runs, digits, hyphens. Candidates only; each is
# checked against the index before it means anything.
QUOTED_RE = re.compile(r"[\"'\u201c\u2018]([^\"'\u201d\u2019]{2,80})[\"'\u201d\u2019]")
IDENT_RE = re.compile(r"\b(?=[\w\-]*[A-Z0-9])[A-Za-z]+[\w]*(?:[-_/][\w]+)+\b")


def _normalise(s: str) -> str:
    """Compare on letters and digits only, so spacing and case stop mattering."""
    return re.sub(r"[^a-z0-9]", "", s.lower())


def _candidates(message: str) -> list[str]:
    found: list[str] = []
    for m in FILENAME_RE.finditer(message):
        found.append(m.group(0).strip())
    for m in QUOTED_RE.finditer(message):
        found.append(m.group(1).strip())
    for m in IDENT_RE.finditer(message):
        found.append(m.group(0).strip())

    seen, out = set(), []
    for c in found:
        k = _normalise(c)
        if k and k not in seen:
            seen.add(k)
            out.append(c)
    return out


def _match(candidate: str, indexed: list[str], attached: list[str]) -> tuple[str, str] | None:
    """
    Returns (matched_name, where) or None.

    Exact normalised match first, then containment either way — someone
    writing the name without its extension, or with extra words around it,
    should still resolve.
    """
    c = _normalise(candidate)
    if not c:
        return None

    for where, names in (("kb", indexed), ("attached", attached)):
        for n in names:
            if _normalise(n) == c:
                return n, where

    # Containment, longest match wins so a specific name beats a generic one.
    best: tuple[str, str] | None = None
    for where, names in (("kb", indexed), ("attached", attached)):
        for n in names:
            nn = _normalise(n)
            if len(c) >= 4 and (c in nn or nn in c):
                if best is None or len(_normalise(best[0])) < len(nn):
                    best = (n, where)
    return best


def resolve_documents(message: str, inventory: list[dict],
                      session_id: str | None = None) -> dict:
    """
    Work out which documents the user is referring to and where they live.

    Returns a verdict the planner is given as fact:

      named          what the user appears to have referred to
      in_kb          resolved names that are indexed and searchable
      attached_only  attached files that are not text-searchable (images)
      missing        named but present nowhere
      kb_available   whether the index has anything at all
      route          "rag" | "vision" | "none"
    """
    indexed = [d["doc"] for d in documents()]
    attached = [i["name"] for i in inventory]

    # Attachments that can be read as text are already indexed by intake;
    # anything else has to be looked at by the vision model.
    image_kinds = {"image", "pdf_scanned"}
    attached_visual = [i["name"] for i in inventory if i["kind"] in image_kinds]

    named = _candidates(message)
    in_kb: list[str] = []
    attached_only: list[str] = []
    missing: list[str] = []

    for c in named:
        hit = _match(c, indexed, attached)
        if hit is None:
            # Only report a miss for something that clearly meant to be a file.
            if FILENAME_RE.fullmatch(c):
                missing.append(c)
            continue
        name, where = hit
        if where == "kb" and name not in in_kb:
            in_kb.append(name)
        elif where == "attached" and name not in attached_only:
            if name in attached_visual:
                attached_only.append(name)
            elif name not in in_kb:
                in_kb.append(name)

    # Route. Indexed text wins over vision — a document that can be read as
    # text should never be looked at as a picture.
    if in_kb:
        route = "rag"
    elif attached_only or attached_visual:
        route = "vision"
    elif indexed and not named:
        # No document named, but there is a corpus. The planner decides
        # whether the question is a knowledge question.
        route = "none"
    else:
        route = "none"

    verdict = {
        "named": named[:8],
        "in_kb": in_kb,
        "attached_only": attached_only or attached_visual,
        "missing": missing,
        "kb_available": bool(indexed),
        "kb_documents": indexed[:40],
        "route": route,
    }

    log_event("rag.resolve", session_id=session_id, named=named[:5],
              in_kb=in_kb, missing=missing, route=route)
    return verdict


def describe(verdict: dict) -> str:
    """One block of plain text, handed to the planner as established fact."""
    lines = []

    if verdict["in_kb"]:
        lines.append(
            "DOCUMENT STATUS: these documents are indexed and searchable: "
            + ", ".join(verdict["in_kb"])
            + ". Use the rag agent, scoped to them. Do NOT use the vision "
              "agent — they are already readable as text.")
    if verdict["attached_only"]:
        lines.append(
            "IMAGE ATTACHMENTS that must be read by the vision agent: "
            + ", ".join(verdict["attached_only"]) + ".")
    if verdict["missing"]:
        lines.append(
            "NOT FOUND: the user referred to "
            + ", ".join(verdict["missing"])
            + ", which is not attached and not in the knowledge base. Say so "
              "rather than planning a step that cannot succeed.")
    if not lines:
        if verdict["kb_available"]:
            lines.append(
                f"KNOWLEDGE BASE: {len(verdict['kb_documents'])} document(s) "
                "indexed. Use the rag agent for any question about procedures, "
                "specifications, equipment or past correspondence.")
        else:
            lines.append("KNOWLEDGE BASE: empty. No documents are indexed.")

    return "\n".join(lines)
