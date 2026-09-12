"""`fver scan`: capture the build, index functions, run the backend front-end."""

from __future__ import annotations

import logging

import typer
from rich.table import Table

from fver.backends.base import BackendToolMissing
from fver.core.models import Claim, PropertyClass, Status
from fver.util.log import console, setup_logging

log = logging.getLogger("fver.scan")


def register(app: typer.Typer) -> None:
    @app.command()
    def scan(
        no_preprocess: bool = typer.Option(
            False, "--no-preprocess", help="Skip compiler preprocessing."
        ),
        no_translate: bool = typer.Option(
            False, "--no-translate", help="Skip the backend front-end."
        ),
        backend: str | None = typer.Option(
            None, "--backend", help="Override the configured backend."
        ),
        verbose: bool = typer.Option(False, "--verbose", "-v"),
    ) -> None:
        """Capture the build, index every function, and find out what the backend can represent."""
        from fver.core.context import AppContext

        ctx = AppContext.load(need_backend=not no_translate, backend_name=backend)
        setup_logging(ctx.ws.logs_dir, verbose=verbose, run_name="scan")
        try:
            run_scan(ctx, preprocess=not no_preprocess, translate=not no_translate)
        finally:
            ctx.close()


def run_scan(ctx, preprocess: bool = True, translate: bool = True, quiet: bool = False) -> dict:
    """The scan pipeline. Returns the index dict that was written to work/index.json."""
    from fver.build.compile_commands import capture_build
    from fver.build.preprocess import preprocess_all
    from fver.build.targets import compare_target, detect_target
    from fver.extract.attack_surface import score
    from fver.extract.callgraph import build_callgraph
    from fver.extract.functions import extract_from_tu

    ws, cfg, ledger = ctx.ws, ctx.config, ctx.ledger
    backend_name = ctx.backend_name
    run_id = ledger.start_run(
        "scan", backend_name, ctx.target.key, {"preprocess": preprocess, "translate": translate}
    )
    ok = False
    try:
        # 1. build capture
        cap = capture_build(
            ws.repo_root, cfg.build, compiler=cfg.target.compiler, work_dir=ws.work_dir
        )
        for w in cap.warnings:
            log.warning(w)
        tus = cap.tus
        if not quiet:
            console.print(f"[bold]Build:[/bold] {len(tus)} translation unit(s) from {cap.source}")

        # 2. preprocess
        pp_errors: dict[str, str] = {}
        if preprocess:
            pp_errors = preprocess_all(ws, tus, timeout=cfg.budget.checker_timeout_seconds)

        # 3. target
        detected = detect_target(cfg.target.compiler)
        for w in compare_target(detected, ctx.target):
            log.warning(w)

        # 4. functions
        functions = []
        source_cache: dict[str, str] = {}
        for tu in tus:
            try:
                fns = extract_from_tu(ws.repo_root, tu)
            except OSError as e:
                log.warning("cannot read %s: %s", tu.source_path, e)
                continue
            functions.extend(fns)
            source_cache[tu.source_path] = (ws.repo_root / tu.source_path).read_text(
                encoding="utf-8", errors="replace"
            )
        cg = build_callgraph(functions)
        for f in functions:
            f.attack_score, f.attack_reasons = score(
                f, source_cache[f.source_path], cg, cg.externals.get(f.id, [])
            )

        # 5. ledger + index
        ledger.upsert_tus(tus)
        ledger.upsert_functions(functions)
        removed = ledger.prune({t.id for t in tus}, {f.id for f in functions})
        if removed:
            log.info("pruned %d function(s) no longer present in the build", removed)
        index = {
            "run_id": run_id,
            "build_source": cap.source,
            "target": ctx.target.key,
            "tus": tus,
            "preprocess_errors": pp_errors,
            "functions": functions,
            "callgraph": cg.edges,
            "externals": cg.externals,
        }

        # 6. backend front-end
        unsupported: dict[str, str] = {}
        tu_errors: dict[str, str] = {}
        if translate and ctx.backend is not None:
            by_tu: dict[str, list] = {}
            for f in functions:
                by_tu.setdefault(f.tu_id, []).append(f)
            try:
                ctx.backend.prepare(tus, ws.repo_root)
            except BackendToolMissing as e:
                raise typer.BadParameter(str(e)) from None
            for tu in tus:
                res = ctx.backend.translate(tu, by_tu.get(tu.id, []), ws.repo_root)
                if res.tu_error:
                    tu_errors[tu.id] = res.tu_error
                for f in by_tu.get(tu.id, []):
                    supported = res.supported.get(f.name, res.tu_error is None)
                    if not supported:
                        reason = res.reasons.get(f.name) or res.tu_error or "unsupported by backend"
                        unsupported[f.id] = reason
                        ledger.mark_unsupported(f.id, backend_name, ctx.target.key, reason, run_id)
                    else:
                        cur = ledger.current_claim(f.id, backend_name, ctx.target.key)
                        if cur is not None and cur.status is Status.UNSUPPORTED:
                            ledger.record_claim(
                                Claim(
                                    function_id=f.id,
                                    property_class=PropertyClass.UB_FREE,
                                    backend=backend_name,
                                    target_key=ctx.target.key,
                                    status=Status.NOT_ATTEMPTED,
                                    body_hash=f.body_hash,
                                    cache_key="",
                                    message="now accepted by the backend front-end",
                                    run_id=run_id,
                                )
                            )
            index["unsupported"] = unsupported
            index["tu_errors"] = tu_errors
        ws.write_state("index", index)

        # 7. proofs whose inputs changed since they were made
        stale = []
        if ctx.backend is not None:
            from fver.agent.invalidate import reconcile

            stale = reconcile(ctx, run_id)
            index["stale"] = [s.function.id for s in stale]
            ws.write_state("index", index)

        if not quiet:
            _print_summary(
                tus, pp_errors, functions, unsupported, translate and ctx.backend is not None
            )
        if stale and not quiet:
            console.print(
                f"[magenta]{len(stale)} previously verified function(s) are now stale[/] "
                "(code or a callee contract changed); `fver verify` will redo them."
            )
        ok = True
        return index
    finally:
        ledger.end_run(run_id, ok, "" if ok else "scan failed")


def _print_summary(tus, pp_errors, functions, unsupported, translated: bool) -> None:
    t = Table(title="fver scan", show_header=False)
    t.add_row("translation units", str(len(tus)))
    t.add_row("preprocessed", f"{len(tus) - len(pp_errors)} ok, {len(pp_errors)} failed")
    t.add_row("functions", str(len(functions)))
    if translated:
        t.add_row("supported by backend", f"{len(functions) - len(unsupported)}")
        t.add_row("unsupported", str(len(unsupported)))
    console.print(t)

    top = sorted(functions, key=lambda f: -f.attack_score)[:10]
    if top:
        t2 = Table(title="Top attack-surface candidates")
        t2.add_column("score", justify="right")
        t2.add_column("function")
        t2.add_column("file")
        t2.add_column("why")
        for f in top:
            t2.add_row(
                f"{f.attack_score:.2f}",
                f.name,
                f"{f.source_path}:{f.start_line}",
                "; ".join(r.split(" ", 1)[1] for r in f.attack_reasons[:3]),
            )
        console.print(t2)
    if unsupported:
        t3 = Table(title="Unsupported functions (first 10)")
        t3.add_column("function")
        t3.add_column("reason")
        for fid, why in list(unsupported.items())[:10]:
            t3.add_row(fid.split(":", 1)[1], why)
        console.print(t3)
