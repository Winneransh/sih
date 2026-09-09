"""
The agent roster.

Built at runtime, not baked into a prompt. If no vision model is installed,
the Vision Agent is not offered to the planner, so it cannot plan a step that
would fail. The system's capability changes with the user's setup.
"""

from __future__ import annotations

from .. import manifest
from .base import Agent, AgentInput, AgentResult
from .calc import CalcAgent
from .code import CodeAgent
from .documents import DocxAgent, PptxAgent, XlsxAgent
from .general import GeneralAgent
from .rag_agent import RagAgent
from .vision import VisionAgent

ALL_AGENTS: dict[str, Agent] = {
    a.name: a() for a in (
        GeneralAgent, VisionAgent, RagAgent, CodeAgent, CalcAgent,
        DocxAgent, PptxAgent, XlsxAgent,
    )
}


def get(name: str) -> Agent:
    if name not in ALL_AGENTS:
        raise KeyError(f"unknown agent: {name}")
    return ALL_AGENTS[name]


def available() -> list[dict]:
    """
    Roster of agents that can actually run — an agent whose required
    capability isn't installed is left out entirely.
    """
    out = []
    for name, agent in ALL_AGENTS.items():
        models = manifest.by_capability(agent.capability)
        # Coding falls back to any text model when no dedicated coder exists.
        if not models and agent.capability == "coding":
            models = manifest.by_capability("text")
        if not models:
            continue
        # The rag agent additionally needs something to embed with.
        if name == "rag" and not manifest.by_capability("embedding"):
            continue
        spec = type(agent).spec()
        spec["usable_models"] = models
        out.append(spec)
    return out


def unavailable() -> list[dict]:
    names = {a["agent"] for a in available()}
    out = []
    for n, a in ALL_AGENTS.items():
        if n in names:
            continue
        missing = a.capability
        if n == "rag" and not manifest.by_capability("embedding"):
            missing = "embedding"
        out.append({"agent": n, "missing_capability": missing})
    return out


__all__ = ["ALL_AGENTS", "get", "available", "unavailable",
           "Agent", "AgentInput", "AgentResult"]
