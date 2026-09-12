"""Choosing and ordering the functions a run works on, plus the run plan."""

from __future__ import annotations

import fnmatch
import json
import os
from pathlib import Path
from typing import Any

from rich.table import Table

from fver.core.context import AppContext
from fver.core.models import FunctionInfo, Status
from fver.prove.client import LLMClient, LLMSettings
from fver.util.log import console


def _select(
    ctx: AppContext,
    names: list[str],
    file_pat: str | None,
    limit: int | None,
    retry_unresolved: bool,
    recheck: bool,
    with_deps: bool = True,
) -> list[FunctionInfo]:
    backend = ctx.backend_name
    tk = ctx.target.key
    # IN_PROGRESS is what a run that died (API error, Ctrl-C) leaves behind;
    # only one run works on a project at a time, so it is picked up again.
    statuses = [Status.NOT_ATTEMPTED, Status.STALE, Status.IN_PROGRESS]
    if retry_unresolved:
        statuses.append(Status.UNRESOLVED)
    if recheck:
        statuses = [Status.VERIFIED]
    rows = []
    for st in statuses:
        rows.extend(ctx.ledger.list_functions(backend, tk, status=st, order_by_attack_score=True))
    seen: set[str] = set()
    out: list[FunctionInfo] = []
    for row in rows:
        fn = row.function
        if fn.id in seen:
            continue
        seen.add(fn.id)
        if names and not any(fnmatch.fnmatchcase(fn.name, n) for n in names):
            continue
        if file_pat and not (
            fnmatch.fnmatch(fn.source_path, file_pat) or fn.source_path.endswith(file_pat)
        ):
            continue
        out.append(fn)
    out.sort(key=lambda f: (-f.attack_score, f.source_path, f.name))
    if with_deps and not recheck:
        out = order_with_dependencies(ctx, out)
    if limit is not None:
        out = out[:limit]
    return out


def order_with_dependencies(ctx: AppContext, selected: list[FunctionInfo]) -> list[FunctionInfo]:
    """Keep the attack-score priority, but place each function's not-yet-verified
    internal callees before it (post-order), so callers are attempted only once
    their callees' contracts exist. Callees outside the selection are pulled in;
    recursion cycles are cut at the first repeat."""
    backend = ctx.backend_name
    tk = ctx.target.key
    ordered: list[FunctionInfo] = []
    placed: set[str] = set()
    verified: dict[str, bool] = {}

    def skip(fn: FunctionInfo) -> bool:
        """Verified callees need no work; unsupported ones cannot be helped;
        unresolved ones already failed and are retried only when named
        (their callers are then blocked at no cost)."""
        if fn.id not in verified:
            claim = ctx.ledger.current_claim(fn.id, backend, tk)
            verified[fn.id] = claim is not None and claim.status in (
                Status.VERIFIED,
                Status.UNSUPPORTED,
                Status.UNRESOLVED,
                Status.BUG_FOUND,
            )
        return verified[fn.id]

    def resolve(caller: FunctionInfo, name: str) -> FunctionInfo | None:
        cands = ctx.ledger.find_functions(name=name)
        if not cands:
            return None
        same_tu = [c for c in cands if c.tu_id == caller.tu_id]
        if same_tu:
            return same_tu[0]
        non_static = [c for c in cands if not c.is_static]
        return non_static[0] if non_static else None

    def visit(fn: FunctionInfo, stack: set[str]) -> None:
        if fn.id in placed or fn.id in stack:
            return
        stack.add(fn.id)
        for callee_name in fn.callees:
            if callee_name == fn.name:
                continue
            callee = resolve(fn, callee_name)
            if callee is None or skip(callee):
                continue
            visit(callee, stack)
        stack.discard(fn.id)
        if fn.id not in placed:
            placed.add(fn.id)
            ordered.append(fn)

    for fn in selected:
        visit(fn, set())
    return ordered


def _print_summary(counts: dict[Status, int], cache_hits: int, usd: float) -> None:
    table = Table(title="verify summary")
    table.add_column("outcome")
    table.add_column("count", justify="right")
    for st in (Status.VERIFIED, Status.BUG_FOUND, Status.UNRESOLVED):
        table.add_row(st.value, str(counts.get(st, 0)))
    table.add_row("cache hits", str(cache_hits))
    table.add_row("LLM cost (USD)", f"{usd:.2f}")
    console.print(table)


def _make_llm(settings: LLMSettings, log_dir: Path, run_id: str) -> Any:
    """Real client by default. FVER_FAKE_LLM=<path to a JSON list of scripted
    responses> substitutes the scripted fake, for offline trials of the whole
    pipeline (never for real verification)."""
    scripted = os.environ.get("FVER_FAKE_LLM")
    if not scripted:
        return LLMClient(settings, log_dir=log_dir, run_id=run_id)
    from fver.prove.client import FakeLLMClient

    responses = json.loads(Path(scripted).read_text(encoding="utf-8", errors="replace"))
    console.print("[yellow]FVER_FAKE_LLM set: using scripted responses, no API calls.[/]")
    return FakeLLMClient(responses=list(responses), settings=settings)
