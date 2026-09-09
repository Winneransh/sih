"""
Calculation Agent.

Engineering calculations are signed off by a person, so the working has to
be visible and the arithmetic has to be right. A language model doing mental
arithmetic is neither.

So the model writes the calculation as executable steps with units, the
sandbox runs it, and the printed working is the deliverable. Every number
in the output was computed, not generated.
"""

from __future__ import annotations

import re

from .. import llm
from ..tools import sandbox
from ..tools.files import work_dir
from .base import Agent, AgentInput, AgentResult

SYSTEM = """Write the calculation as Python that prints its working.

Output only code in one ```python block.
Print each step on its own line: GIVEN, FORMULA, SUBSTITUTION, then RESULT with units.
Print only numbers the code computed — never a figure you worked out yourself.
Use only the values supplied. If a value needed for the calculation is
missing, print MISSING: <what is needed> and stop rather than assuming a
typical value. State every assumption you do make as an ASSUMPTION line."""


PRELUDE = """
try:
    import sympy as sp
except ImportError:
    sp = None
try:
    from pint import UnitRegistry
    ureg = UnitRegistry()
    Q_ = ureg.Quantity
except ImportError:
    ureg = None
    Q_ = None
"""


def extract_code(text: str) -> str:
    m = re.search(r"```(?:python|py)?\s*\n(.*?)```", text, re.S)
    return (m.group(1) if m else text).strip()


class CalcAgent(Agent):
    name = "calc"
    capability = "text"
    description = (
        "Performs engineering calculations by generating and executing them, "
        "printing every step: given values, formula, substitution, intermediates "
        "and the final result with units. Output is auditable — every number is "
        "computed, not asserted."
    )
    input_kind = "calculation request with given values"
    output_kind = "worked steps + result"

    MAX_ATTEMPTS = 3

    def execute(self, inp: AgentInput) -> AgentResult:
        task = inp.task or inp.payload.get("calculation", "")
        if not task:
            return AgentResult(self.name, False, "no calculation supplied",
                               error="calc agent needs a calculation")

        wd = work_dir(inp.session_id) / "calc"
        wd.mkdir(parents=True, exist_ok=True)

        given = inp.payload.get("given")
        prompt = f"Calculation required:\n{task}"
        if given:
            prompt += f"\n\nGiven values:\n{given}"

        raw = llm.chat(prompt, system=SYSTEM, model=inp.model,
                       capability="text", session_id=inp.session_id,
                       max_tokens=2500, temperature=0.1)
        code = PRELUDE + "\n" + extract_code(raw)

        result = None
        for i in range(self.MAX_ATTEMPTS):
            # sympy/pint aren't in the network-less Docker image, so the
            # calc path uses the subprocess backend where they're installed.
            result = sandbox.run_python(code, session_id=inp.session_id,
                                        workdir=wd, timeout=60,
                                        force_subprocess=True)
            if result["ok"]:
                break
            if i == self.MAX_ATTEMPTS - 1:
                break
            fix = llm.chat(
                f"This calculation failed:\n\n{code}\n\nError:\n{result['stderr'][:2000]}\n\n"
                "Return the corrected complete code in one ```python block.",
                system=SYSTEM, model=inp.model, capability="text",
                session_id=inp.session_id, max_tokens=2500, temperature=0.1)
            code = PRELUDE + "\n" + extract_code(fix)

        steps = (result["stdout"] or "").strip()
        final = self._final_line(steps)
        ok = bool(result and result["ok"] and steps)

        path = self.spill(inp.session_id, "calculation_steps.txt", steps)

        return AgentResult(
            agent=self.name, ok=ok,
            summary=(f"Calculation completed. {final}" if ok
                     else f"Calculation failed: {result['stderr'][:300]}"),
            data={
                "steps": steps[:3000],
                "result_line": final,
                "code": code[:2000],
                "stderr": (result["stderr"] or "")[:800],
            },
            artifacts=[path],
            model_used=inp.model,
        )

    def _final_line(self, stdout: str) -> str:
        for line in reversed(stdout.splitlines()):
            if line.strip().upper().startswith("RESULT"):
                return line.strip()
        lines = [l for l in stdout.splitlines() if l.strip()]
        return lines[-1].strip() if lines else ""
