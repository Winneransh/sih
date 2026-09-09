"""
Code Agent.

The only agent with a genuine internal loop: write, run in the sandbox,
read the error, fix, run again. That loop is bounded and lives inside the
agent, not in the plan — the planner never expresses loops, because a small
model asked to plan a loop produces nonsense.

Each attempt is logged, which is what makes the iteration requirement
visible in the UI rather than merely claimed.
"""

from __future__ import annotations

import re

from .. import llm
from ..audit import log_event
from ..tools import sandbox
from ..tools.files import outputs_dir, work_dir
from .base import Agent, AgentInput, AgentResult

SYSTEM = """Write correct, self-contained Python.

Output only code in one ```python block, no explanation.
It must run standalone with no network and no arguments.
Standard library only unless told otherwise. Print results so they can be checked.
Implement only what was asked. Do not add extra features, configuration
options or error handling that was not requested."""


FIX_TEMPLATE = """Fix this code.

--- CODE ---
{code}

--- STDERR ---
{stderr}

--- STDOUT ---
{stdout}

Return the complete corrected file in one ```python block."""


def extract_code(text: str) -> str:
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


class CodeAgent(Agent):
    name = "code"
    capability = "coding"
    description = (
        "Writes Python for internal tools and scripts, runs it in an isolated "
        "sandbox with no network, reads any error, fixes it and re-runs until "
        "it works. Returns working code plus the verified output."
    )
    input_kind = "description of what to build"
    output_kind = "working code + run output"

    MAX_ATTEMPTS = 4

    def execute(self, inp: AgentInput) -> AgentResult:
        task = inp.task or inp.payload.get("description", "")
        if not task:
            return AgentResult(self.name, False, "no task supplied",
                               error="code agent needs a description")

        wd = work_dir(inp.session_id) / "code"
        wd.mkdir(parents=True, exist_ok=True)

        context = self._context(inp)
        prompt = f"{context}Write Python that does the following:\n\n{task}"

        raw = llm.chat(prompt, system=SYSTEM, model=inp.model,
                       capability="coding", session_id=inp.session_id,
                       max_tokens=3000, temperature=0.2)
        code = extract_code(raw)

        attempts = []
        result = None

        for i in range(1, self.MAX_ATTEMPTS + 1):
            result = sandbox.run_python(code, session_id=inp.session_id,
                                        workdir=wd,
                                        timeout=int(inp.options.get("timeout", 60)))
            attempts.append({
                "attempt": i,
                "ok": result["ok"],
                "exit_code": result["exit_code"],
                "stderr": (result["stderr"] or "")[:600],
                "stdout": (result["stdout"] or "")[:600],
            })
            log_event("agent.code.attempt", session_id=inp.session_id,
                      attempt=i, ok=result["ok"], backend=result["backend"])

            if result["ok"]:
                break
            if i == self.MAX_ATTEMPTS:
                break

            fix = llm.chat(
                FIX_TEMPLATE.format(code=code,
                                    stderr=result["stderr"][:2500],
                                    stdout=result["stdout"][:1000]),
                system=SYSTEM, model=inp.model, capability="coding",
                session_id=inp.session_id, max_tokens=3000, temperature=0.2)
            code = extract_code(fix)

        filename = inp.options.get("filename", "solution.py")
        out_path = outputs_dir(inp.session_id) / filename
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_text(code, encoding="utf-8")

        ok = bool(result and result["ok"])
        summary = (
            f"Code {'ran successfully' if ok else 'still failing'} after "
            f"{len(attempts)} attempt(s) in the {result['backend']} sandbox."
        )

        return AgentResult(
            agent=self.name, ok=ok, summary=summary,
            data={
                "code": code[:3000],
                "stdout": (result["stdout"] or "")[:2000],
                "stderr": (result["stderr"] or "")[:1000],
                "attempts": attempts,
                "sandbox_backend": result["backend"],
            },
            artifacts=[str(out_path)],
            model_used=inp.model,
        )

    def _context(self, inp: AgentInput) -> str:
        prior = inp.context.get("prior_summaries") or []
        if not prior:
            return ""
        return "Context from earlier steps:\n" + "\n".join(
            f"- {p}" for p in prior[-4:]) + "\n\n"
