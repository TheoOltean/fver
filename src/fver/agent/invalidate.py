"""Proof invalidation: keep the ledger honest after code changes.

A proof depends on exactly (function body, callee contracts, external specs,
backend + tool versions, target). The ledger derives STALE automatically when
the body hash changes. This module covers the other inputs: it recomputes
each verified function's cache key and records an explicit STALE claim when
the key no longer matches, and it marks callers stale when a function's
accepted contract changes.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass

from fver.agent import store
from fver.core.context import AppContext
from fver.core.models import Claim, Cost, FunctionInfo, PropertyClass, Status

log = logging.getLogger(__name__)


@dataclass
class StaleEntry:
    function: FunctionInfo
    reason: str


def _stale_claim(
    function: FunctionInfo, backend: str, target_key: str, reason: str, run_id: str, prev: Claim
) -> Claim:
    return Claim(
        function_id=function.id,
        property_class=PropertyClass(prev.property_class),
        backend=backend,
        target_key=target_key,
        status=Status.STALE,
        body_hash=function.body_hash,
        cache_key=prev.cache_key,
        proof_hash=prev.proof_hash,
        tool_versions=dict(prev.tool_versions),
        assumptions=list(prev.assumptions),
        message=f"stale: {reason}",
        cost=Cost(),
        run_id=run_id,
        extra={"previous_status": prev.status.value},
    )


def reconcile(ctx: AppContext, run_id: str) -> list[StaleEntry]:
    """Record STALE claims for every function whose proof inputs changed.

    Body changes are already visible through the derived status; they get an
    explicit claim here so the history shows when and why. Contract and tool
    changes are detected by recomputing the cache key.
    """
    if ctx.backend is None:
        return []
    from fver.agent.loop import Verifier

    backend = ctx.backend.name
    tk = ctx.target.key
    verifier = Verifier(ctx, llm=None, run_id=run_id)
    stale: list[StaleEntry] = []
    for row in ctx.ledger.list_functions(backend, tk, order_by_attack_score=False):
        claim = row.claim
        if claim is None:
            continue
        if row.status is Status.STALE and claim.status is not Status.STALE:
            reason = "function body changed"
        elif row.status is Status.VERIFIED:
            try:
                key = verifier.cache_key_for(verifier.build_task(row.function))
            except Exception as e:  # noqa: BLE001 - a missing TU or file means: re-verify
                key, reason = "", f"could not recompute proof inputs ({e})"
            if key == claim.cache_key:
                continue
            if key:
                reason = "a callee contract, external spec or tool version changed"
        else:
            continue
        ctx.ledger.record_claim(_stale_claim(row.function, backend, tk, reason, run_id, claim))
        stale.append(StaleEntry(row.function, reason))
        log.info("[%s] marked stale: %s", row.function.name, reason)
    return stale


def invalidate_dependents(
    ctx: AppContext,
    function: FunctionInfo,
    old_contract: str | None,
    new_contract: str,
    run_id: str,
) -> list[StaleEntry]:
    """After a function is (re-)verified with a different contract, its callers'
    proofs were checked against the old contract and must be redone."""
    if old_contract is None or old_contract.strip() == new_contract.strip():
        return []
    backend = ctx.backend_name
    tk = ctx.target.key
    stale: list[StaleEntry] = []
    for dep in ctx.ledger.dependents(function.name):
        claim = ctx.ledger.current_claim(dep.id, backend, tk)
        if claim is None or claim.status is not Status.VERIFIED:
            continue
        reason = f"contract of callee {function.name} changed"
        ctx.ledger.record_claim(_stale_claim(dep, backend, tk, reason, run_id, claim))
        stale.append(StaleEntry(dep, reason))
        log.info("[%s] marked stale: %s", dep.name, reason)
    return stale


def previous_contract(ctx: AppContext, function: FunctionInfo) -> str | None:
    if ctx.backend is None:
        return None
    loaded = store.load_accepted(ctx.ws, function.source_path, function.name)
    if loaded is None:
        return None
    return ctx.backend.extract_spec(loaded[0], function)
