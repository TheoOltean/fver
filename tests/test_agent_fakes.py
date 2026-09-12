"""Fakes shared by the agent tests. No tests in this file."""

from __future__ import annotations

import uuid
from collections.abc import Iterable
from dataclasses import dataclass, field
from pathlib import Path

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
from fver.core.config import FverConfig
from fver.core.context import AppContext
from fver.core.models import (
    Claim,
    Finding,
    FunctionInfo,
    Status,
    Target,
    TranslationUnit,
    sha256_text,
)
from fver.core.workspace import Workspace
from fver.ledger.api import FunctionRow, Summary


class FakeBackend:
    """Verdict is chosen by markers in the submission text."""

    name = "fake"

    def __init__(self, audit_fail: bool = False, tool_error: bool = False):
        self.audit_fail = audit_fail
        self.tool_error = tool_error
        self.checks: list[Submission] = []

    def tool_versions(self) -> dict[str, str]:
        return {"fake": "1.0"}

    def prepare(self, tus, repo_root: Path) -> None:
        pass

    def translate(self, tu, functions, repo_root) -> TranslateResult:
        return TranslateResult(supported={f.name: True for f in functions})

    def submission_spec(self) -> SubmissionSpec:
        return SubmissionSpec(
            files={"function.c": "annotated function"},
            required=["function.c"],
            language_hints={"function.c": "c"},
        )

    def prompt_context(self) -> PromptContext:
        return PromptContext(
            reference="FAKE REFERENCE",
            examples="FAKE EXAMPLES",
            instructions="Annotate with [[fake::...]].",
            forbidden_patterns=[r"fake::trust_me", r"Admitted\."],
        )

    def guardrail(self, submission: Submission) -> list[str]:
        if any("FVER_CHEAT" in c for c in submission.files.values()):
            return ["submission narrows precondition to false (FVER_CHEAT)"]
        return []

    def check(
        self, task: FunctionTask, submission: Submission, timeout_seconds: int
    ) -> CheckResult:
        self.checks.append(submission)
        text = "\n".join(submission.files.values())
        if self.tool_error:
            return CheckResult(CheckOutcome.TOOL_ERROR, "checker binary missing")
        if "FVER_ACCEPT" in text:
            return CheckResult(
                CheckOutcome.OK,
                "ok",
                proof_hash="ph-" + sha256_text(text)[:8],
                assumptions=["libc:memcpy"],
                tool_versions={"fake": "1.0"},
            )
        if "FVER_GOALS" in text:
            return CheckResult(CheckOutcome.GOALS_REMAIN, "goals left", goals=["0 <= i < n"])
        if "FVER_BUG" in text:
            return CheckResult(CheckOutcome.BUG, "OOB at line 3", witness="n = -1")
        return CheckResult(CheckOutcome.AUTOMATION_STUCK, "stuck at loop invariant")

    def audit(self, task: FunctionTask, result: CheckResult) -> AuditResult:
        if self.audit_fail:
            return AuditResult(False, violations=["axiom smuggled: my_axiom"])
        return AuditResult(True, assumptions=["axiom:functional_extensionality"])

    def extract_spec(self, submission: Submission, function: FunctionInfo) -> str:
        src = submission.files.get("function.c", "")
        return "\n".join(ln for ln in src.splitlines() if "[[fake::" in ln)


@dataclass
class FakeLedger:
    tus: dict[str, TranslationUnit] = field(default_factory=dict)
    functions: dict[str, FunctionInfo] = field(default_factory=dict)
    claims: list[Claim] = field(default_factory=list)
    finds: list[Finding] = field(default_factory=list)
    runs: list[dict] = field(default_factory=list)

    def close(self) -> None:
        pass

    def start_run(self, command, backend, target_key, meta) -> str:
        rid = uuid.uuid4().hex[:8]
        self.runs.append({"id": rid, "command": command, "ok": None})
        return rid

    def end_run(self, run_id, ok, message="") -> None:
        for r in self.runs:
            if r["id"] == run_id:
                r["ok"] = ok

    def upsert_tus(self, tus: Iterable[TranslationUnit]) -> None:
        for t in tus:
            self.tus[t.id] = t

    def upsert_functions(self, functions: Iterable[FunctionInfo]) -> None:
        for f in functions:
            self.functions[f.id] = f

    def get_tu(self, tu_id):
        return self.tus.get(tu_id)

    def get_function(self, function_id):
        return self.functions.get(function_id)

    def find_functions(self, name=None, source_path=None):
        return [
            f
            for f in self.functions.values()
            if (name is None or f.name == name)
            and (source_path is None or f.source_path == source_path)
        ]

    def current_claim(self, function_id, backend, target_key):
        for c in reversed(self.claims):
            if c.function_id == function_id and c.backend == backend and c.target_key == target_key:
                return c
        return None

    def list_functions(
        self, backend, target_key, status=None, order_by_attack_score=True, limit=None
    ):
        rows = []
        for f in self.functions.values():
            c = self.current_claim(f.id, backend, target_key)
            st = c.status if c else Status.NOT_ATTEMPTED
            if status is None or st is status:
                rows.append(FunctionRow(f, st, c))
        if order_by_attack_score:
            rows.sort(key=lambda r: -r.function.attack_score)
        return rows[:limit] if limit else rows

    def record_claim(self, claim: Claim) -> None:
        self.claims.append(claim)

    def claims_for(self, function_id):
        return [c for c in self.claims if c.function_id == function_id]

    def mark_unsupported(self, function_id, backend, target_key, reason, run_id) -> None:
        pass

    def record_finding(self, finding: Finding) -> None:
        self.finds.append(finding)

    def findings(self, function_id=None, source_path=None):
        return list(self.finds)

    def summary(self, backend, target_key) -> Summary:
        raise NotImplementedError

    def dependents(self, function_name):
        return [f for f in self.functions.values() if function_name in f.callees]


SAMPLE_C = """#include <stddef.h>

static int helper(int x) {
    return x + 1;
}

void zero(char *buf, size_t n) {
    for (size_t i = 0; i < n; i++)
        buf[i] = 0;
}

int use(char *buf, size_t n) {
    zero(buf, n);
    return helper(3);
}
"""


def make_repo(
    tmp_path: Path,
) -> tuple[Workspace, FakeLedger, FunctionInfo, FunctionInfo, FunctionInfo]:
    repo = tmp_path / "repo"
    (repo / "src").mkdir(parents=True)
    (repo / "src" / "a.c").write_text(SAMPLE_C)
    cfg = FverConfig()
    cfg.project.backend = "fake"
    cfg.budget.max_attempts_per_function = 4
    ws = Workspace.create(repo, cfg)
    ledger = FakeLedger()
    tu = TranslationUnit(
        id="tu1", source_path="src/a.c", directory=".", arguments=["clang", "-c", "src/a.c"]
    )
    ledger.upsert_tus([tu])
    lines = SAMPLE_C.split("\n")

    def fn(name, start, end, callees, score, static=False):
        body = "\n".join(lines[start - 1 : end])
        return FunctionInfo(
            id=FunctionInfo.make_id("tu1", name),
            name=name,
            tu_id="tu1",
            source_path="src/a.c",
            start_line=start,
            end_line=end,
            signature=lines[start - 1].rstrip(" {"),
            body_hash=sha256_text(body),
            is_static=static,
            callees=callees,
            attack_score=score,
        )

    helper = fn("helper", 3, 5, [], 0.1, static=True)
    zero = fn("zero", 7, 10, [], 0.9)
    use = fn("use", 12, 15, ["zero", "helper"], 0.5)
    ledger.upsert_functions([helper, zero, use])
    return ws, ledger, helper, zero, use


def make_ctx(ws: Workspace, ledger: FakeLedger, backend: FakeBackend) -> AppContext:
    return AppContext(ws=ws, config=ws.config, ledger=ledger, target=Target(), backend=backend)


def sub(text: str, name: str = "function.c") -> str:
    return f"Here is my submission.\n\n```c file={name}\n{text}\n```\n"
