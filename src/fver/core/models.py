"""Domain model shared by every part of fver.

Nothing in this module knows about any particular proof backend, build system
or LLM. It is the vocabulary the CLI, the ledger, the proof loop and the backends
use to talk to each other.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import UTC, datetime
from enum import Enum
from pathlib import Path
from typing import Any


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


# ---------------------------------------------------------------------------
# Build / target
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class Target:
    """The platform a build is for. Implementation-defined behaviour is fixed
    by these choices, so every verification result is relative to a Target."""

    triple: str = "x86_64-linux-gnu"
    compiler: str = "cc"
    int_bits: int = 32
    long_bits: int = 64
    pointer_bits: int = 64
    char_signed: bool = True
    little_endian: bool = True

    @property
    def key(self) -> str:
        return (
            f"{self.triple}|{self.compiler}|int{self.int_bits}|long{self.long_bits}"
            f"|ptr{self.pointer_bits}|{'s' if self.char_signed else 'u'}char"
            f"|{'le' if self.little_endian else 'be'}"
        )


@dataclass
class TranslationUnit:
    """One C file and the flags it is read with."""

    id: str
    source_path: str  # relative to repo root, posix separators
    directory: str  # working directory of the compile command
    arguments: list[str]  # full compiler argv
    preprocessed_path: str | None = None  # relative to workspace root

    @staticmethod
    def make_id(source_path: str, arguments: list[str]) -> str:
        return sha256_text(source_path + "\0" + "\0".join(arguments))[:16]


# ---------------------------------------------------------------------------
# Functions
# ---------------------------------------------------------------------------


@dataclass
class FunctionInfo:
    """A function definition found in a translation unit."""

    id: str  # f"{tu_id}:{name}"
    name: str
    tu_id: str
    source_path: str  # relative to repo root
    start_line: int  # 1-based, inclusive
    end_line: int  # 1-based, inclusive
    signature: str
    body_hash: str  # sha256 of the normalised definition text
    is_static: bool = False
    callees: list[str] = field(default_factory=list)
    attack_score: float = 0.0
    attack_reasons: list[str] = field(default_factory=list)

    @staticmethod
    def make_id(tu_id: str, name: str) -> str:
        return f"{tu_id}:{name}"


# ---------------------------------------------------------------------------
# Claims and status
# ---------------------------------------------------------------------------


class Status(str, Enum):
    """Where a function stands in the ledger. Ordered roughly by progress."""

    UNSUPPORTED = "unsupported"  # the backend cannot represent this function
    NOT_ATTEMPTED = "not_attempted"
    IN_PROGRESS = "in_progress"
    UNRESOLVED = "unresolved"  # attempted, budget spent, no proof
    STALE = "stale"  # a result exists but the code or a callee contract changed since
    BUG_FOUND = "bug_found"  # a hunter or the checker found a concrete UB witness
    VERIFIED = "verified"  # proof accepted and audited


class PropertyClass(str, Enum):
    """What kind of claim. Only UB_FREE is in scope today; the enum exists so
    the ledger schema does not change when more are added."""

    UB_FREE = "ub_free"


@dataclass
class Cost:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_read_tokens: int = 0
    cache_write_tokens: int = 0
    usd: float = 0.0
    wall_seconds: float = 0.0
    llm_calls: int = 0
    checker_runs: int = 0

    def add(self, other: Cost) -> Cost:
        return Cost(
            self.input_tokens + other.input_tokens,
            self.output_tokens + other.output_tokens,
            self.cache_read_tokens + other.cache_read_tokens,
            self.cache_write_tokens + other.cache_write_tokens,
            self.usd + other.usd,
            self.wall_seconds + other.wall_seconds,
            self.llm_calls + other.llm_calls,
            self.checker_runs + other.checker_runs,
        )


@dataclass
class Claim:
    """A single verification result for one function under one backend and
    target. The ledger stores the history of these; the newest per function
    is that function's status."""

    function_id: str
    property_class: PropertyClass
    backend: str
    target_key: str
    status: Status
    body_hash: str
    cache_key: str  # see fver.ledger.cache
    proof_hash: str | None = None
    tool_versions: dict[str, str] = field(default_factory=dict)
    assumptions: list[str] = field(default_factory=list)  # trusted specs/axioms relied on
    message: str = ""  # short human-readable summary
    cost: Cost = field(default_factory=Cost)
    created_at: str = field(default_factory=now_iso)
    run_id: str = ""
    extra: dict[str, Any] = field(default_factory=dict)


@dataclass
class Finding:
    """A concrete bug or suspicious behaviour found by a hunter or checker."""

    function_id: str | None
    source_path: str
    line: int | None
    kind: str  # e.g. "out_of_bounds", "signed_overflow", "uninitialised_read"
    tool: str
    message: str
    witness: str = ""  # reproducer / counterexample text if any
    # "high": reached from a real entry point or a concrete test (a genuine bug).
    # "low": found by analysing a function with unconstrained inputs; may be a
    # precondition the callers always satisfy. Only high-confidence findings
    # become BUG_FOUND claims.
    confidence: str = "high"
    created_at: str = field(default_factory=now_iso)
    run_id: str = ""


# ---------------------------------------------------------------------------
# ---------------------------------------------------------------------------


def rel_posix(path: Path, root: Path) -> str:
    """Repo-relative posix path used as the canonical file identifier."""
    return path.resolve().relative_to(root.resolve()).as_posix()
