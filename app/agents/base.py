"""
Agent base.

Every agent is the same shape: one LLM, a small set of function tools,
one input, one bounded output. No agent plans, spawns another agent, or
knows the others exist. Cardinality and ordering are the planner's job;
this class just runs one unit of work.

The output cap is the load-bearing rule. Bulk content is written to disk
and travels as a path, so the planner's context stays small no matter how
many documents are being processed.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path

from ..audit import Timer, log_event
from ..config import MAX_AGENT_RESULT_CHARS
from ..tools.files import work_dir


@dataclass
class AgentInput:
    session_id: str
    task: str                            # what the planner wants done
    payload: dict = field(default_factory=dict)   # files, prior results
    model: str | None = None             # planner's model choice
    context: dict = field(default_factory=dict)   # request + prior summaries
    options: dict = field(default_factory=dict)   # template, mode, etc.


@dataclass
class AgentResult:
    agent: str
    ok: bool
    summary: str                         # short, human-readable
    data: dict = field(default_factory=dict)      # structured, bounded
    artifacts: list[str] = field(default_factory=list)   # file paths produced
    sources: list[dict] = field(default_factory=list)    # citation carriers
    error: str | None = None
    model_used: str | None = None

    def to_dict(self) -> dict:
        return {
            "agent": self.agent,
            "ok": self.ok,
            "summary": self.summary,
            "data": self.data,
            "artifacts": self.artifacts,
            "sources": self.sources,
            "error": self.error,
            "model_used": self.model_used,
        }


class Agent:
    name: str = "base"
    capability: str = "text"
    description: str = ""
    input_kind: str = "any"
    output_kind: str = "any"

    def run(self, inp: AgentInput) -> AgentResult:
        with Timer(f"agent.{self.name}", session_id=inp.session_id,
                   task=inp.task[:200], model=inp.model):
            try:
                result = self.execute(inp)
            except Exception as e:
                log_event(f"agent.{self.name}.failed",
                          session_id=inp.session_id, error=str(e))
                return AgentResult(agent=self.name, ok=False,
                                   summary=f"{self.name} failed: {e}",
                                   error=str(e))
        return self._bound(result, inp)

    def execute(self, inp: AgentInput) -> AgentResult:
        raise NotImplementedError

    # -------------------------------------------------------------- helpers

    def _bound(self, result: AgentResult, inp: AgentInput) -> AgentResult:
        """
        Enforce the size cap. Anything oversized is spilled to disk and
        replaced by a pointer, so the planner never sees bulk content.
        """
        blob = json.dumps(result.data, default=str)
        if len(blob) > MAX_AGENT_RESULT_CHARS:
            spill = work_dir(inp.session_id) / f"{self.name}_{id(result)}.json"
            spill.write_text(blob, encoding="utf-8")
            result.artifacts.append(str(spill))
            result.data = {
                "_spilled": True,
                "_path": str(spill),
                "_size_chars": len(blob),
                "_preview": blob[:800],
            }
            log_event("agent.spill", session_id=inp.session_id,
                      agent=self.name, path=str(spill), chars=len(blob))

        if len(result.summary) > 1200:
            result.summary = result.summary[:1200] + " ..."
        return result

    def spill(self, session_id: str, name: str, text: str) -> str:
        """Write bulk text to the session work dir and return its path."""
        p = work_dir(session_id) / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
        return str(p)

    @classmethod
    def spec(cls) -> dict:
        """What the planner is told about this agent."""
        return {
            "agent": cls.name,
            "capability_required": cls.capability,
            "description": cls.description,
            "input": cls.input_kind,
            "output": cls.output_kind,
        }
