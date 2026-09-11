"""Ledger interface: the bookkeeping of what is proven, what is not, and why.

The ledger is backend-agnostic. It stores TranslationUnits, FunctionInfos,
Claims (one per verification attempt outcome) and Findings (bugs). The
newest Claim for a (function, backend, target) is that function's status.

The SQLite implementation lives in fver.ledger.sqlite. Nothing else should
import sqlite3.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from typing import Protocol

from fver.core.models import Claim, Cost, Finding, FunctionInfo, Status, TranslationUnit


@dataclass
class FunctionRow:
    """A function joined with its current status (for listings)."""

    function: FunctionInfo
    status: Status
    claim: Claim | None


@dataclass
class Summary:
    total_functions: int
    by_status: dict[str, int]
    verified_weighted: float  # attack-score-weighted fraction verified, 0..1
    total_cost: Cost
    findings: int


class Ledger(Protocol):
    # -- lifecycle
    def close(self) -> None: ...

    # -- runs (one per CLI invocation that changes state)
    def start_run(self, command: str, backend: str, target_key: str, meta: dict) -> str:
        """Returns run_id."""
        ...

    def end_run(self, run_id: str, ok: bool, message: str = "") -> None: ...

    # -- build index
    def upsert_tus(self, tus: Iterable[TranslationUnit]) -> None: ...
    def upsert_functions(self, functions: Iterable[FunctionInfo]) -> None: ...
    def prune(self, keep_tu_ids: set[str], keep_function_ids: set[str]) -> int:
        """Remove TUs and functions no longer present in the latest scan
        (their claims and findings are kept as history). Returns the number
        of functions removed."""
        ...

    def get_tu(self, tu_id: str) -> TranslationUnit | None: ...
    def get_function(self, function_id: str) -> FunctionInfo | None: ...
    def find_functions(
        self, name: str | None = None, source_path: str | None = None
    ) -> list[FunctionInfo]: ...
    def list_functions(
        self,
        backend: str,
        target_key: str,
        status: Status | None = None,
        order_by_attack_score: bool = True,
        limit: int | None = None,
    ) -> list[FunctionRow]: ...

    # -- claims
    def record_claim(self, claim: Claim) -> None: ...
    def current_claim(self, function_id: str, backend: str, target_key: str) -> Claim | None: ...
    def claims_for(self, function_id: str) -> list[Claim]: ...
    def mark_unsupported(
        self, function_id: str, backend: str, target_key: str, reason: str, run_id: str
    ) -> None: ...

    # -- findings
    def record_finding(self, finding: Finding) -> None: ...
    def delete_findings(self, source_paths: set[str], tool: str | None = None) -> int:
        """Drop findings for these files (optionally only from one tool) so a
        fresh hunt run replaces, rather than accumulates on, the previous one."""
        ...

    def findings(
        self, function_id: str | None = None, source_path: str | None = None
    ) -> list[Finding]: ...

    # -- reporting
    def summary(self, backend: str, target_key: str) -> Summary: ...
    def dependents(self, function_name: str) -> list[FunctionInfo]:
        """Functions that call `function_name` (used for invalidation)."""
        ...


def open_ledger(path) -> Ledger:
    from fver.ledger.sqlite import SqliteLedger

    return SqliteLedger(path)
