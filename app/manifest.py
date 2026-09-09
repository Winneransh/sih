"""
The manifest is the single source of truth about installed models.

It is written by the downloader (structural facts) and enriched from the
README frontmatter (capabilities, licence). No LLM is involved — the
frontmatter is already structured, and an mmproj file on disk is harder
evidence of vision than any prose description.

The planner reads this file to decide which model serves each step.
"""

import json
import re
from pathlib import Path

import yaml

from .config import MANIFEST_PATH

PIPELINE_CAPS = {
    "text-generation": ["text"],
    "image-text-to-text": ["text", "vision"],
    "visual-question-answering": ["text", "vision"],
    "feature-extraction": ["embedding"],
    "sentence-similarity": ["embedding"],
    "text-ranking": ["reranker"],
    "automatic-speech-recognition": ["audio"],
}


def load() -> dict:
    if not MANIFEST_PATH.exists():
        return {}
    return json.loads(MANIFEST_PATH.read_text())


def save(m: dict) -> None:
    MANIFEST_PATH.write_text(json.dumps(m, indent=2))


def get(repo_id: str) -> dict | None:
    return load().get(repo_id)


def resolve(name: str) -> dict:
    """Accept a full repo id or an unambiguous fragment of one."""
    m = load()
    if name in m:
        return m[name]
    hits = [k for k in m if name.lower() in k.lower()]
    if len(hits) == 1:
        return m[hits[0]]
    if len(hits) > 1:
        raise ValueError(f"ambiguous model name '{name}': {', '.join(hits)}")
    raise ValueError(f"model not installed: {name}")


def parse_frontmatter(readme: Path) -> dict:
    if not readme.exists():
        return {}
    text = readme.read_text(encoding="utf-8", errors="replace")
    m = re.match(r"^---\s*\n(.*?)\n---\s*\n", text, re.S)
    if not m:
        return {}
    try:
        return yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return {}


def infer_capabilities(fm: dict, entry: dict) -> list[str]:
    caps: list[str] = []

    tag = fm.get("pipeline_tag")
    if tag in PIPELINE_CAPS:
        caps.extend(PIPELINE_CAPS[tag])

    # An mmproj file is hard evidence, regardless of what the card claims.
    if entry.get("mmproj_file") and "vision" not in caps:
        caps.append("vision")

    hay = " ".join(str(t) for t in (fm.get("tags") or []))
    hay = (hay + " " + entry.get("repo_id", "")).lower()

    if "coder" in hay or "-code" in hay:
        caps.append("coding")
    if "embed" in hay and "embedding" not in caps:
        caps.append("embedding")
    if "rerank" in hay and "reranker" not in caps:
        caps.append("reranker")

    if not caps:
        caps = ["text"]

    seen, out = set(), []
    for c in caps:
        if c not in seen:
            seen.add(c)
            out.append(c)
    return out


def enrich_all() -> dict:
    """Fill capabilities/licence/tags for every installed model."""
    m = load()
    for repo, entry in m.items():
        fm = parse_frontmatter(Path(entry["path"]) / "README.md")
        entry["capabilities"] = infer_capabilities(fm, entry)
        entry["pipeline_tag"] = fm.get("pipeline_tag")
        entry["license"] = fm.get("license")
        base = fm.get("base_model")
        entry["base_model"] = base[0] if isinstance(base, list) and base else base
        entry["hf_tags"] = [str(t) for t in (fm.get("tags") or [])][:15]
        entry["enriched"] = True
    save(m)
    return m


def for_planner() -> list[dict]:
    """
    Compact view handed to the planner. Deliberately small — the planner
    needs enough to choose between models, not the whole record.
    """
    out = []
    for repo, e in load().items():
        out.append({
            "model": repo,
            "capabilities": e.get("capabilities", []),
            "size_gb": round((e.get("size_bytes") or 0) / 1024**3, 1),
            "quant": e.get("quant"),
            "params_hint": e.get("base_model") or repo,
        })
    return out


def by_capability(cap: str) -> list[str]:
    return [r for r, e in load().items() if cap in (e.get("capabilities") or [])]
