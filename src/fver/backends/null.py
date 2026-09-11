"""A fake backend for tests and dry runs.

It needs no external tools and decides outcomes from marker words in the
submission text, so the whole pipeline (scan, verify loop, ledger, reports)
can be exercised offline.

  FVER_ACCEPT -> OK            FVER_GOALS -> GOALS_REMAIN
  FVER_BUG    -> BUG           FVER_CHEAT -> guardrail violation
  anything else -> AUTOMATION_STUCK
  FVER_AXIOM in an artifact -> audit fails
Functions whose name contains "unsupported" are reported as unsupported.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fver.backends.base import (
    AuditResult,
    CheckOutcome,
    CheckResult,
    FunctionTask,
    PromptContext,
    Submission,
    SubmissionSpec,
    TranslateResult,
)
from fver.core.models import FunctionInfo, Target, ToolStatus, TranslationUnit

ACCEPT = "FVER_ACCEPT"
GOALS = "FVER_GOALS"
BUG = "FVER_BUG"
CHEAT = "FVER_CHEAT"
AXIOM = "FVER_AXIOM"


class NullBackend:
    name = "null"

    def __init__(self, workspace_dir: Path, settings: dict[str, Any], target: Target):
        self.workspace_dir = Path(workspace_dir)
        self.workspace_dir.mkdir(parents=True, exist_ok=True)
        self.settings = dict(settings or {})
        self.target = target
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def _rec(self, method: str, **kw: Any) -> None:
        self.calls.append((method, kw))

    def doctor(self) -> list[ToolStatus]:
        self._rec("doctor")
        return [ToolStatus(name="null", found=True, path="(builtin)", version="0", required=False)]

    def tool_versions(self) -> dict[str, str]:
        return {"null": "0"}

    def prepare(self, tus: list[TranslationUnit], repo_root: Path) -> None:
        self._rec("prepare", tus=[t.id for t in tus])

    def translate(
        self, tu: TranslationUnit, functions: list[FunctionInfo], repo_root: Path
    ) -> TranslateResult:
        self._rec("translate", tu=tu.id, functions=[f.name for f in functions])
        supported = {f.name: "unsupported" not in f.name for f in functions}
        reasons = {
            f.name: "name contains 'unsupported'" for f in functions if not supported[f.name]
        }
        return TranslateResult(supported=supported, reasons=reasons)

    def submission_spec(self) -> SubmissionSpec:
        return SubmissionSpec(
            files={"function.c": "the annotated function"},
            required=["function.c"],
            language_hints={"function.c": "c"},
        )

    def prompt_context(self) -> PromptContext:
        return PromptContext(
            reference=f"Null backend. Write the function back unchanged and add the word {ACCEPT} in a comment.",
            examples=f"```c file=function.c\n/* {ACCEPT} */\nint f(void) {{ return 0; }}\n```",
            instructions="Return exactly one fenced block: ```c file=function.c ... ```.",
            forbidden_patterns=[CHEAT],
        )

    def guardrail(self, submission: Submission) -> list[str]:
        self._rec("guardrail")
        text = "\n".join(submission.files.values())
        out = []
        if CHEAT in text:
            out.append(f"submission contains {CHEAT}")
        if "function.c" not in submission.files:
            out.append("missing function.c")
        return out

    def check(
        self, task: FunctionTask, submission: Submission, timeout_seconds: int
    ) -> CheckResult:
        self._rec("check", function=task.function.name)
        problems = self.guardrail(submission)
        if problems:
            return CheckResult(outcome=CheckOutcome.GUARDRAIL, feedback="; ".join(problems))
        text = "\n".join(submission.files.values())
        wd = Path(task.workdir)
        wd.mkdir(parents=True, exist_ok=True)
        art = wd / "function.c"
        art.write_text(submission.files.get("function.c", ""), encoding="utf-8")
        artifacts = {"function.c": str(art)}
        versions = self.tool_versions()
        if ACCEPT in text:
            return CheckResult(
                outcome=CheckOutcome.OK,
                feedback="accepted",
                artifacts=artifacts,
                proof_hash=submission.content_hash(),
                assumptions=["null-backend"],
                tool_versions=versions,
            )
        if BUG in text:
            return CheckResult(
                outcome=CheckOutcome.BUG,
                feedback="undefined behaviour witnessed",
                witness="x = 0 -> out of bounds",
                artifacts=artifacts,
                tool_versions=versions,
            )
        if GOALS in text:
            return CheckResult(
                outcome=CheckOutcome.GOALS_REMAIN,
                feedback="unsolved goal: 0 <= n",
                goals=["n : nat\n============================\n0 <= n"],
                artifacts=artifacts,
                tool_versions=versions,
            )
        return CheckResult(
            outcome=CheckOutcome.AUTOMATION_STUCK,
            feedback=f"annotation missing; include {ACCEPT}",
            artifacts=artifacts,
            tool_versions=versions,
        )

    def audit(self, task: FunctionTask, result: CheckResult) -> AuditResult:
        self._rec("audit", function=task.function.name)
        for path in result.artifacts.values():
            p = Path(path)
            if p.exists() and AXIOM in p.read_text(encoding="utf-8", errors="replace"):
                return AuditResult(passed=False, violations=[f"{p.name} contains {AXIOM}"])
        return AuditResult(passed=True, assumptions=list(result.assumptions))

    def extract_spec(self, submission: Submission, function: FunctionInfo) -> str:
        text = submission.files.get("function.c", "")
        brace = text.find("{")
        return (text[:brace].strip() if brace >= 0 else text.strip()) + ";"
