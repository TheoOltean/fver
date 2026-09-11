"""In-memory ledger: same semantics as the SQLite one, no persistence.

Used by tests across the project and as the reference implementation of
the ledger semantics. `summarize_rows` is shared with the SQLite ledger so
both compute coverage identically.
"""

from __future__ import annotations

import threading
import uuid
from collections.abc import Iterable
from dataclasses import replace

from fver.core.models import Claim, Cost, Finding, FunctionInfo, Status, TranslationUnit, now_iso
from fver.ledger.api import FunctionRow, Summary

# Every function counts at least this much toward weighted coverage, so a
# codebase with no attack-surface ranking still gets a meaningful number.
WEIGHT_FLOOR = 0.05


def new_run_id() -> str:
    return uuid.uuid4().hex[:12]


def derive_status(function: FunctionInfo, claim: Claim | None) -> Status:
    """A function's effective status. A claim made against a different body
    hash than the function's current one is stale: the code changed since."""
    if claim is None:
        return Status.NOT_ATTEMPTED
    if (
        claim.status in (Status.VERIFIED, Status.UNRESOLVED, Status.BUG_FOUND, Status.IN_PROGRESS)
        and claim.body_hash
        and claim.body_hash != function.body_hash
    ):
        return Status.STALE
    return claim.status


def summarize_rows(rows: Iterable[FunctionRow], findings_count: int) -> Summary:
    """Compute a Summary from function rows (with their current claims)."""
    by_status: dict[str, int] = {s.value: 0 for s in Status}
    total = 0
    weight_sum = 0.0
    verified_weight = 0.0
    cost = Cost()
    for row in rows:
        total += 1
        by_status[row.status.value] = by_status.get(row.status.value, 0) + 1
        w = max(row.function.attack_score, WEIGHT_FLOOR)
        weight_sum += w
        if row.status is Status.VERIFIED:
            verified_weight += w
        if row.claim is not None:
            cost = cost.add(row.claim.cost)
    frac = verified_weight / weight_sum if weight_sum > 0 else 0.0
    return Summary(
        total_functions=total,
        by_status=by_status,
        verified_weighted=frac,
        total_cost=cost,
        findings=findings_count,
    )


def unsupported_claim(
    function: FunctionInfo, backend: str, target_key: str, reason: str, run_id: str
) -> Claim:
    from fver.core.models import PropertyClass

    return Claim(
        function_id=function.id,
        property_class=PropertyClass.UB_FREE,
        backend=backend,
        target_key=target_key,
        status=Status.UNSUPPORTED,
        body_hash=function.body_hash,
        cache_key="",
        message=reason,
        run_id=run_id,
    )


class InMemoryLedger:
    """Dictionary-backed Ledger. Thread-safe via a single lock."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._runs: dict[str, dict] = {}
        self._tus: dict[str, TranslationUnit] = {}
        self._functions: dict[str, FunctionInfo] = {}
        self._claims: list[Claim] = []
        self._findings: list[Finding] = []

    # -- lifecycle -----------------------------------------------------------

    def close(self) -> None:
        return None

    # -- runs ------------------------------------------------------------------

    def start_run(self, command: str, backend: str, target_key: str, meta: dict) -> str:
        run_id = new_run_id()
        with self._lock:
            self._runs[run_id] = {
                "id": run_id,
                "command": command,
                "backend": backend,
                "target_key": target_key,
                "started_at": now_iso(),
                "ended_at": None,
                "ok": None,
                "message": "",
                "meta": dict(meta),
            }
        return run_id

    def end_run(self, run_id: str, ok: bool, message: str = "") -> None:
        with self._lock:
            run = self._runs.get(run_id)
            if run is not None:
                run.update(ended_at=now_iso(), ok=ok, message=message)

    # -- build index -----------------------------------------------------------

    def upsert_tus(self, tus: Iterable[TranslationUnit]) -> None:
        with self._lock:
            for tu in tus:
                self._tus[tu.id] = replace(tu, arguments=list(tu.arguments))

    def prune(self, keep_tu_ids: set[str], keep_function_ids: set[str]) -> int:
        with self._lock:
            gone = [fid for fid in self._functions if fid not in keep_function_ids]
            for fid in gone:
                del self._functions[fid]
            for tid in [t for t in self._tus if t not in keep_tu_ids]:
                del self._tus[tid]
            return len(gone)

    def upsert_functions(self, functions: Iterable[FunctionInfo]) -> None:
        with self._lock:
            for fn in functions:
                self._functions[fn.id] = replace(
                    fn, callees=list(fn.callees), attack_reasons=list(fn.attack_reasons)
                )

    def get_tu(self, tu_id: str) -> TranslationUnit | None:
        with self._lock:
            return self._tus.get(tu_id)

    def get_function(self, function_id: str) -> FunctionInfo | None:
        with self._lock:
            return self._functions.get(function_id)

    def find_functions(
        self, name: str | None = None, source_path: str | None = None
    ) -> list[FunctionInfo]:
        with self._lock:
            out = [
                f
                for f in self._functions.values()
                if (name is None or f.name == name)
                and (source_path is None or f.source_path == source_path)
            ]
        out.sort(key=lambda f: (f.source_path, f.start_line))
        return out

    def list_functions(
        self,
        backend: str,
        target_key: str,
        status: Status | None = None,
        order_by_attack_score: bool = True,
        limit: int | None = None,
    ) -> list[FunctionRow]:
        with self._lock:
            rows = [self._row(f, backend, target_key) for f in self._functions.values()]
        if status is not None:
            rows = [r for r in rows if r.status is status]
        if order_by_attack_score:
            rows.sort(
                key=lambda r: (
                    -r.function.attack_score,
                    r.function.source_path,
                    r.function.start_line,
                )
            )
        else:
            rows.sort(key=lambda r: (r.function.source_path, r.function.start_line))
        if limit is not None:
            rows = rows[:limit]
        return rows

    def _row(self, fn: FunctionInfo, backend: str, target_key: str) -> FunctionRow:
        claim = self._current(fn.id, backend, target_key)
        return FunctionRow(function=fn, status=derive_status(fn, claim), claim=claim)

    # -- claims ----------------------------------------------------------------

    def record_claim(self, claim: Claim) -> None:
        with self._lock:
            self._claims.append(claim)

    def _current(self, function_id: str, backend: str, target_key: str) -> Claim | None:
        # Newest by insertion order among equal timestamps.
        best: Claim | None = None
        for c in self._claims:
            if c.function_id != function_id or c.backend != backend or c.target_key != target_key:
                continue
            if best is None or c.created_at >= best.created_at:
                best = c
        return best

    def current_claim(self, function_id: str, backend: str, target_key: str) -> Claim | None:
        with self._lock:
            return self._current(function_id, backend, target_key)

    def claims_for(self, function_id: str) -> list[Claim]:
        with self._lock:
            return [c for c in self._claims if c.function_id == function_id]

    def mark_unsupported(
        self, function_id: str, backend: str, target_key: str, reason: str, run_id: str
    ) -> None:
        fn = self.get_function(function_id)
        if fn is None:
            raise KeyError(f"unknown function {function_id}")
        self.record_claim(unsupported_claim(fn, backend, target_key, reason, run_id))

    # -- findings --------------------------------------------------------------

    def record_finding(self, finding: Finding) -> None:
        with self._lock:
            self._findings.append(finding)

    def delete_findings(self, source_paths: set[str], tool: str | None = None) -> int:
        with self._lock:
            before = len(self._findings)
            self._findings = [
                f
                for f in self._findings
                if not (f.source_path in source_paths and (tool is None or f.tool == tool))
            ]
            return before - len(self._findings)

    def findings(
        self, function_id: str | None = None, source_path: str | None = None
    ) -> list[Finding]:
        with self._lock:
            return [
                f
                for f in self._findings
                if (function_id is None or f.function_id == function_id)
                and (source_path is None or f.source_path == source_path)
            ]

    # -- reporting -------------------------------------------------------------

    def summary(self, backend: str, target_key: str) -> Summary:
        rows = self.list_functions(backend, target_key, order_by_attack_score=False)
        return summarize_rows(rows, len(self.findings()))

    def dependents(self, function_name: str) -> list[FunctionInfo]:
        with self._lock:
            out = [f for f in self._functions.values() if function_name in f.callees]
        out.sort(key=lambda f: (f.source_path, f.start_line))
        return out
