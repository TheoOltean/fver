"""`fver prove [TARGET...]`: the one verb.

Point it at nothing (the whole repository), a file, a function, or globs of
either. It brings the index up to date if sources changed, runs CBMC over
the selected functions first (a concrete bug is recorded and that function
is not sent to the prover), then proves the rest in dependency order under
the configured budget.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import typer

from fver.core.context import AppContext
from fver.core.models import FunctionInfo, Status
from fver.prove.client import FatalAgentError, LLMSettings
from fver.prove.loop import FunctionOutcome, Verifier
from fver.prove.select import _make_llm, _print_summary, _select
from fver.util.log import console, setup_logging


def split_targets(ctx: AppContext, targets: list[str]) -> tuple[list[str], list[str]]:
    """(function name globs, file globs). A target is a file if it names an
    existing path, contains a slash, or ends in .c/.h; otherwise a function."""
    names: list[str] = []
    files: list[str] = []
    for t in targets:
        p = Path(t)
        if (
            "/" in t
            or t.endswith((".c", ".h"))
            or (ctx.ws.repo_root / t).exists()
            or (p.is_absolute() and p.exists())
        ):
            if p.is_absolute():
                t = ctx.ws.relpath(p)
            files.append(t)
        else:
            names.append(t)
    return names, files


def select_functions(ctx: AppContext, targets: list[str], limit: int | None) -> list[FunctionInfo]:
    """Union of the selections for every target, callees first, then limited.
    Naming a function explicitly also retries one left unresolved."""
    from fver.prove.select import order_with_dependencies

    names, files = split_targets(ctx, targets)
    retry = bool(names)
    if not files:
        return _select(ctx, names, None, limit, retry, False, with_deps=True)
    seen: set[str] = set()
    out: list[FunctionInfo] = []
    for pat in files:
        for fn in _select(ctx, names, pat, None, retry, False, with_deps=False):
            if fn.id not in seen:
                seen.add(fn.id)
                out.append(fn)
    out.sort(key=lambda f: (-f.attack_score, f.source_path, f.name))
    out = order_with_dependencies(ctx, out)
    return out[:limit] if limit else out


def refresh_index(ctx: AppContext) -> bool:
    """Index when there is no index yet or a .c/.h file changed since the
    last one. Returns True if it ran."""
    from fver.index.scan import run_scan, sources_fingerprint

    index = ctx.ws.read_state("index")
    if index and index.get("fingerprint") == sources_fingerprint(ctx.ws):
        return False
    console.print("[bold]Indexing[/] (sources changed or no index yet) ...")
    run_scan(ctx, preprocess=True, translate=ctx.backend is not None, quiet=True)
    return True


def hunt_first(
    ctx: AppContext, selected: list[FunctionInfo], whole_repo: bool
) -> list[FunctionInfo]:
    """CBMC over the selection (plus the sanitizers over the project's tests
    for a whole-repository run). Functions with a confirmed bug drop out."""
    from fver.prove.hunt import run_hunt

    if not selected:
        return selected
    findings = run_hunt(ctx, None, None, None, selected=selected, quiet=True)
    bugs = {f.function_id for f in findings if f.function_id and f.confidence == "high"}
    kept = []
    for fn in selected:
        claim = ctx.ledger.current_claim(fn.id, ctx.backend_name, ctx.target.key)
        if fn.id in bugs or (claim is not None and claim.status is Status.BUG_FOUND):
            console.print(
                f"[red]bug_found   [/] {fn.name}  {fn.source_path} (CBMC; not sent to the prover)"
            )
            continue
        kept.append(fn)
    return kept


def run_prove(
    ctx: AppContext,
    targets: list[str],
    *,
    on_done: Callable[[FunctionInfo, FunctionOutcome], None] | None = None,
) -> int:
    """The pipeline. Returns the process exit code."""
    cfg = ctx.config
    assert ctx.backend is not None
    refresh_index(ctx)
    selected = select_functions(ctx, targets, cfg.budget.max_functions_per_run or None)
    if not selected:
        console.print(
            "[yellow]Nothing to prove:[/] no matching functions are waiting. "
            "`fver status` shows where everything stands."
        )
        return 0
    settings = LLMSettings(
        api_key=cfg.model.api_key,
        workspace_id=cfg.model.workspace_id,
        base_url=cfg.model.base_url,
        model=cfg.model.model,
        effort=cfg.model.effort,
        max_tokens=cfg.model.max_tokens,
        prompt_caching=cfg.model.prompt_caching,
        timeout_seconds=cfg.model.timeout_seconds,
    )
    budget_run = cfg.budget.max_usd_per_run
    parallelism = cfg.budget.parallelism

    selected = hunt_first(ctx, selected, whole_repo=not targets)
    if not selected:
        console.print("Every selected function has a confirmed bug; nothing to prove.")
        return 0

    run_id = ctx.ledger.start_run(
        "prove",
        ctx.backend.name,
        ctx.target.key,
        {
            "targets": targets,
            "model": settings.model,
            "effort": settings.effort,
            "functions": len(selected),
            "max_usd": budget_run,
        },
    )
    verifier = Verifier(ctx, _make_llm(settings, ctx.ws.logs_dir, run_id), run_id)
    console.print(
        f"Proving {len(selected)} function(s) with {settings.model} (effort {settings.effort}), "
        f"budget ${budget_run:.2f}, {parallelism} in parallel."
    )
    counts = {s: 0 for s in Status}
    cache_hits = 0

    def done(fn: FunctionInfo, out: FunctionOutcome) -> None:
        nonlocal cache_hits
        counts[out.claim.status] = counts.get(out.claim.status, 0) + 1
        cache_hits += 1 if out.cache_hit else 0
        if on_done is not None:
            on_done(fn, out)
        else:
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

    code = 0
    try:
        verifier.run(selected, budget_run, parallelism=parallelism, recheck=False, on_done=done)
        ctx.ledger.end_run(run_id, True)
    except FatalAgentError as e:
        console.print(f"[red]Fatal:[/] {e}")
        ctx.ledger.end_run(run_id, False, str(e))
        code = 2
    finally:
        if on_done is None:
            _print_summary(counts, cache_hits, verifier.run_cost.usd)
    return code


def register(app: typer.Typer) -> None:
    @app.command("prove")
    def prove(
        targets: list[str] = typer.Argument(
            None,
            help="Nothing for the whole repository, or files, functions and globs of either.",
            show_default=False,
        ),  # noqa: B008
    ) -> None:
        """Prove functions free of undefined behaviour: index if needed, check them with CBMC first, then prove in dependency order. One line per function as it finishes."""
        ctx = AppContext.load(need_backend=True)
        setup_logging(ctx.ws.logs_dir, run_name="prove")
        try:
            code = run_prove(ctx, list(targets or []))
        finally:
            ctx.close()
        raise typer.Exit(code)
