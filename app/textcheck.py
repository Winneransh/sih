"""
Degenerate-output detection.

Small models given a long or contradictory prompt fail in a characteristic
way: they echo the instructions back, or repeat one sentence until they hit
the token cap. That output is worse than nothing — it looks like content,
so it flows through the pipeline and ends up in a document.

These checks catch it at the agent boundary, so a bad generation is reported
as a failure rather than passed on as data.
"""

from __future__ import annotations

import re
from collections import Counter


def repetition_ratio(text: str) -> float:
    """Fraction of lines that are duplicates. 0.0 = all unique."""
    lines = [l.strip() for l in text.splitlines() if len(l.strip()) > 20]
    if len(lines) < 4:
        return 0.0
    counts = Counter(lines)
    repeated = sum(c - 1 for c in counts.values() if c > 1)
    return repeated / len(lines)


def ngram_repetition(text: str, n: int = 8) -> float:
    """Fraction of repeated word n-grams. Catches looping within a paragraph."""
    words = text.split()
    if len(words) < n * 4:
        return 0.0
    grams = [" ".join(words[i:i + n]) for i in range(len(words) - n)]
    counts = Counter(grams)
    repeated = sum(c - 1 for c in counts.values() if c > 1)
    return repeated / len(grams)


def echoes_prompt(text: str, prompt: str) -> bool:
    """True when the output is largely the instructions handed to the model."""
    if not prompt:
        return False
    # Compare on distinctive phrases rather than whole strings.
    frags = [f.strip() for f in re.split(r"[.\n]", prompt) if len(f.strip()) > 25]
    if not frags:
        return False
    hits = sum(1 for f in frags if f.lower() in text.lower())
    return hits >= max(2, len(frags) // 2)


def is_degenerate(text: str, prompt: str = "") -> tuple[bool, str]:
    """Returns (degenerate, reason)."""
    t = (text or "").strip()
    if not t:
        return True, "empty output"
    if len(t) < 3:
        return True, "output too short"

    r = repetition_ratio(t)
    if r > 0.5:
        return True, f"{int(r * 100)}% of lines are duplicates"

    g = ngram_repetition(t)
    if g > 0.4:
        return True, f"{int(g * 100)}% repeated phrases — model looped"

    if echoes_prompt(t, prompt):
        return True, "output repeats the instructions back"

    return False, ""


def strip_repeats(text: str) -> str:
    """
    Salvage pass. Drops duplicated lines while preserving order, so partially
    looped output keeps whatever unique content it produced before degrading.
    """
    seen, out = set(), []
    for line in text.splitlines():
        key = line.strip()
        if len(key) > 20:
            if key in seen:
                continue
            seen.add(key)
        out.append(line)
    return "\n".join(out).strip()
