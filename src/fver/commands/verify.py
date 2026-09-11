"""`fver verify`: run the LLM proof loop over unverified functions."""

from __future__ import annotations

import fnmatch
import json
import os
import time
from pathlib import Path
from typing import Any

import typer
from rich.table import Table

from fver.agent.client import FatalAgentError, LLMClient, LLMSettings
from fver.agent.loop import FunctionOutcome, Verifier
from fver.agent.pricing import estimate_attempt_usd
from fver.core.context import AppContext
from fver.core.models import FunctionInfo, Status
from fver.util.log import console, setup_logging


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
    statuses = [Status.NOT_ATTEMPTED, Status.STALE]
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
        """Verified callees need no work; unsupported ones cannot be helped."""
        if fn.id not in verified:
            claim = ctx.ledger.current_claim(fn.id, backend, tk)
            verified[fn.id] = claim is not None and claim.status in (
                Status.VERIFIED,
                Status.UNSUPPORTED,
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


def register(app: typer.Typer) -> None:
    @app.command("verify")
    def verify(
        function: list[str] = typer.Option(
            None, "--function", "-f", help="Function name (glob); repeatable."
        ),  # noqa: B008
        file: str | None = typer.Option(
            None, "--file", help="Only functions in this source file (glob)."
        ),
        limit: int | None = typer.Option(
            None, "--limit", "-n", help="Stop after this many functions."
        ),
        max_usd: float | None = typer.Option(
            None, "--max-usd", help="Run budget in USD (overrides config)."
        ),
        retry_unresolved: bool = typer.Option(
            False, "--retry-unresolved", help="Also retry UNRESOLVED functions."
        ),
        recheck: bool = typer.Option(
            False, "--recheck", help="Re-run the checker on VERIFIED functions, no LLM."
        ),
        no_deps: bool = typer.Option(
            False,
            "--no-deps",
            help="Do not pull a function's unverified callees in front of it.",
        ),
        dry_run: bool = typer.Option(
            False, "--dry-run", help="Show the plan and cost estimate; do nothing."
        ),
        parallel: int | None = typer.Option(
            None, "--parallel", "-j", help="Functions verified concurrently."
        ),
        model: str | None = typer.Option(None, "--model", help="Override the model id."),
        effort: str | None = typer.Option(
            None, "--effort", help="low | medium | high | xhigh | max"
        ),
        verbose: bool = typer.Option(False, "--verbose", "-v"),
    ) -> None:
        """Run the LLM proof loop over functions that are not yet verified."""
        ctx = AppContext.load(need_backend=True)
        run_name = f"verify-{time.strftime('%Y%m%d-%H%M%S')}"
        setup_logging(ctx.ws.logs_dir, verbose=verbose, run_name=run_name)
        cfg = ctx.config
        assert ctx.backend is not None

        selected = _select(
            ctx, function or [], file, limit, retry_unresolved, recheck, with_deps=not no_deps
        )
        if not selected:
            console.print(
                "[yellow]Nothing to do:[/] no matching functions in the selected statuses. "
                "Run `fver scan` first, or use --retry-unresolved."
            )
            ctx.close()
            raise typer.Exit(0)

        settings = LLMSettings(
            api_key=cfg.model.api_key,
            base_url=cfg.model.base_url,
            model=model or cfg.model.model,
            effort=effort or cfg.model.effort,
            max_tokens=cfg.model.max_tokens,
            fallbacks=cfg.model.fallbacks,
            prompt_caching=cfg.model.prompt_caching,
            timeout_seconds=cfg.model.timeout_seconds,
        )
        budget_run = max_usd if max_usd is not None else cfg.budget.max_usd_per_run
        parallelism = parallel if parallel is not None else cfg.budget.parallelism

        if dry_run:
            _print_plan(ctx, selected, settings, budget_run, recheck)
            ctx.close()
            raise typer.Exit(0)

        run_id = ctx.ledger.start_run(
            "verify",
            ctx.backend.name,
            ctx.target.key,
            {
                "model": settings.model,
                "effort": settings.effort,
                "functions": len(selected),
                "recheck": recheck,
                "max_usd": budget_run,
            },
        )
        llm = _make_llm(settings, ctx.ws.logs_dir, run_id)
        verifier = Verifier(ctx, llm, run_id)
        console.print(
            f"Verifying {len(selected)} function(s) with {settings.model} (effort {settings.effort}), "
            f"backend {ctx.backend.name}, run budget ${budget_run:.2f}, parallel {parallelism}."
        )

        counts = {s: 0 for s in Status}
        cache_hits = 0

        def on_done(fn: FunctionInfo, out: FunctionOutcome) -> None:
            nonlocal cache_hits
            counts[out.claim.status] = counts.get(out.claim.status, 0) + 1
            if out.cache_hit:
                cache_hits += 1
            colour = {Status.VERIFIED: "green", Status.BUG_FOUND: "red"}.get(
                out.claim.status, "yellow"
            )
            tag = (
                " (cache)"
                if out.cache_hit
                else f" ({out.attempts} attempt(s), ${out.claim.cost.usd:.2f})"
            )
            console.print(
                f"[{colour}]{out.claim.status.value:12}[/] {fn.name}  {fn.source_path}{tag}"
            )

        exit_code = 0
        try:
            verifier.run(
                selected, budget_run, parallelism=parallelism, recheck=recheck, on_done=on_done
            )
            ctx.ledger.end_run(run_id, True)
        except FatalAgentError as e:
            console.print(f"[red]Fatal:[/] {e}")
            ctx.ledger.end_run(run_id, False, str(e))
            exit_code = 2
        finally:
            _print_summary(counts, cache_hits, verifier.run_cost.usd)
            ctx.close()
        raise typer.Exit(exit_code)


def _print_plan(
    ctx: AppContext,
    selected: list[FunctionInfo],
    settings: LLMSettings,
    budget_run: float,
    recheck: bool,
) -> None:
    from fver.agent import store
    from fver.agent.loop import Verifier

    class _NoLLM:
        def complete(self, *a, **k):  # pragma: no cover
            raise RuntimeError("dry run")

    v = Verifier(ctx, _NoLLM(), run_id="dry-run")
    per_attempt = estimate_attempt_usd(settings.model)
    avg_attempts = max(1, min(3, ctx.config.budget.max_attempts_per_function))
    table = Table(title="verify plan (dry run)")
    table.add_column("function")
    table.add_column("file")
    table.add_column("score", justify="right")
    table.add_column("callees w/ contract", justify="right")
    table.add_column("cache")
    hits = 0
    for fn in selected:
        task = v.build_task(fn)
        key = v.cache_key_for(task)
        hit = store.cache_lookup(ctx.ws, key) is not None
        hits += hit
        table.add_row(
            fn.name,
            fn.source_path,
            f"{fn.attack_score:.2f}",
            f"{len(task.callee_specs)}/{len(fn.callees)}",
            "hit" if hit else "-",
        )
    console.print(table)
    n_llm = len(selected) - hits
    est = 0.0 if recheck else n_llm * per_attempt * avg_attempts
    console.print(
        f"{len(selected)} function(s), {hits} cache hit(s). Estimated LLM cost "
        f"~${est:.2f} at ~${per_attempt:.2f}/attempt x ~{avg_attempts} attempts "
        f"(run cap ${budget_run:.2f})."
    )


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
    from fver.agent.client import FakeLLMClient

    responses = json.loads(Path(scripted).read_text(encoding="utf-8", errors="replace"))
    console.print("[yellow]FVER_FAKE_LLM set: using scripted responses, no API calls.[/]")
    return FakeLLMClient(responses=list(responses), settings=settings)
