"""The backend contract.

A backend is the only part of fver that knows about a particular proof
system (RefinedC on Rocq today). Everything else -- build capture, function
extraction, the ledger, the LLM loop, the CLI -- talks to it only through
this interface, so swapping proof systems means writing a new backend and
nothing else.

Vocabulary
----------
Submission   what the LLM produces for one function: a set of named text
             files (e.g. {"source.c": annotated C, "extra.v": helper lemmas}).
             The backend declares which files it expects via `submission_spec`.
CheckResult  what the backend says about a submission. `outcome` is the
             coarse verdict; `feedback` is the text the LLM sees next.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from pathlib import Path
from typing import Protocol, runtime_checkable

from fver.core.models import FunctionInfo, Target, ToolStatus, TranslationUnit


class BackendToolMissing(RuntimeError):
    """The backend's external tool is not installed or not working. Raised
    instead of degrading silently; `fver setup` installs everything."""


class CheckOutcome(str, Enum):
    OK = "ok"  # proof accepted by the checker
    AUTOMATION_STUCK = "automation_stuck"  # the annotations are wrong/insufficient
    GOALS_REMAIN = "goals_remain"  # leftover proof goals need manual proof text
    FRONTEND_ERROR = "frontend_error"  # parse / unsupported construct / bad annotation syntax
    GUARDRAIL = "guardrail"  # submission used a forbidden escape hatch (admit, trust_me, ...)
    BUG = "bug"  # checker produced a concrete UB witness
    TOOL_ERROR = "tool_error"  # the backend itself failed (missing binary, crash, timeout)


@dataclass
class SubmissionSpec:
    """Which files the LLM must produce for this backend, and how they are
    described in the prompt."""

    files: dict[str, str]  # filename -> one-line description
    required: list[str]  # filenames that must be present
    language_hints: dict[str, str] = field(default_factory=dict)  # filename -> fence language


@dataclass
class Submission:
    files: dict[str, str]  # filename -> content
    note: str = ""  # free text the LLM wrote alongside (its reasoning summary)

    def content_hash(self) -> str:
        from fver.core.models import sha256_text

        return sha256_text("\0".join(f"{k}\n{v}" for k, v in sorted(self.files.items())))


@dataclass
class CheckResult:
    outcome: CheckOutcome
    feedback: str  # what the LLM should read next; concise, actionable
    goals: list[str] = field(default_factory=list)  # leftover goals, pretty-printed
    stdout: str = ""
    stderr: str = ""
    artifacts: dict[str, str] = field(default_factory=dict)  # name -> path (inside .fver)
    proof_hash: str | None = None  # stable hash of the accepted proof artifacts
    assumptions: list[str] = field(default_factory=list)  # axioms / trusted specs relied on
    tool_versions: dict[str, str] = field(default_factory=dict)
    duration_seconds: float = 0.0
    witness: str = ""  # for BUG: the counterexample

    @property
    def ok(self) -> bool:
        return self.outcome is CheckOutcome.OK


@dataclass
class TranslateResult:
    """Result of running the backend front-end over a translation unit."""

    supported: dict[str, bool]  # function name -> can this backend represent it?
    reasons: dict[str, str] = field(default_factory=dict)  # function name -> why not
    tu_error: str | None = None  # whole-TU failure (parse error etc.)
    artifacts: dict[str, str] = field(default_factory=dict)


@dataclass
class FunctionTask:
    """Everything a backend needs to check one function."""

    function: FunctionInfo
    tu: TranslationUnit
    target: Target
    repo_root: Path
    workdir: Path  # backend-private scratch dir for this function (inside .fver)
    source_text: str  # the original source file, unmodified
    function_text: str  # just this function's definition
    callee_specs: dict[str, str]  # callee name -> its accepted annotations (this backend's syntax)
    external_specs: dict[str, str]  # external name -> trusted spec text
    previous: Submission | None = None  # last accepted submission for this body, if any


@dataclass
class PromptContext:
    """Backend-supplied material for the LLM prompt. `reference` is large and
    stable (cached); `instructions` is short and specific."""

    reference: str  # language reference, annotation syntax, semantics notes
    examples: str  # worked examples of accepted submissions
    instructions: str  # what to produce, in what format, what is forbidden
    forbidden_patterns: list[str] = field(default_factory=list)  # regexes, for the guardrail


@dataclass
class AuditResult:
    passed: bool
    assumptions: list[str] = field(default_factory=list)
    violations: list[str] = field(default_factory=list)


@runtime_checkable
class Backend(Protocol):
    """Implement this to plug in a proof system.

    Backends are constructed with (workspace_dir, settings, target) where
    workspace_dir is <repo>/.fver/backend/<name>/ and settings is the
    [backend.<name>] table from config.toml.
    """

    name: str

    def doctor(self) -> list[ToolStatus]:
        """Report which external tools are present."""
        ...

    def tool_versions(self) -> dict[str, str]: ...

    def prepare(self, tus: list[TranslationUnit], repo_root: Path) -> None:
        """One-time project setup inside the backend dir (e.g. `refinedc init`)."""
        ...

    def translate(
        self, tu: TranslationUnit, functions: list[FunctionInfo], repo_root: Path
    ) -> TranslateResult:
        """Run the front-end over a TU and report per-function support."""
        ...

    def submission_spec(self) -> SubmissionSpec: ...

    def prompt_context(self) -> PromptContext: ...

    def guardrail(self, submission: Submission) -> list[str]:
        """Return a list of violations (empty = fine). Cheap, runs before check."""
        ...

    def check(
        self, task: FunctionTask, submission: Submission, timeout_seconds: int
    ) -> CheckResult:
        """Write the submission into the backend project and run the checker."""
        ...

    def audit(self, task: FunctionTask, result: CheckResult) -> AuditResult:
        """After OK: verify no smuggled axioms; compute the assumption set."""
        ...

    def extract_spec(self, submission: Submission, function: FunctionInfo) -> str:
        """The part of an accepted submission that callers depend on (the
        function's contract), used as callee_specs for other functions."""
        ...


class BackendError(RuntimeError):
    pass
