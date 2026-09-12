"""Index the sources: list them, preprocess, extract functions, score, run the
backend front-end to learn which functions it accepts."""

from __future__ import annotations

import logging

import typer

from fver.backends.base import BackendToolMissing
from fver.core.models import Claim, PropertyClass, Status
from fver.util.log import console

log = logging.getLogger("fver.scan")


def sources_fingerprint(ws) -> str:
    """Changes whenever any .c or .h file outside .fver/ is added, removed,
    resized or touched: cheap staleness check for the index."""
    from fver.core.models import sha256_text

    parts = []
    for p in sorted(ws.repo_root.rglob("*.[ch]")):
        if ws.is_inside_workspace(p) or any(
            x.startswith(".") for x in p.relative_to(ws.repo_root).parts
        ):
            continue
        st = p.stat()
        parts.append(f"{p.relative_to(ws.repo_root).as_posix()}\0{st.st_size}\0{st.st_mtime_ns}")
    return sha256_text("\n".join(parts))


def run_scan(ctx, preprocess: bool = True, translate: bool = True, quiet: bool = False) -> dict:
    """The scan pipeline. Returns the index dict that was written to work/index.json."""
    from fver.index.attack_surface import score
    from fver.index.callgraph import build_callgraph
    from fver.index.functions import extract_from_tu
    from fver.index.preprocess import preprocess_all
    from fver.index.sources import list_sources

    ws, cfg, ledger = ctx.ws, ctx.config, ctx.ledger
    backend_name = ctx.backend_name
    run_id = ledger.start_run(
        "scan", backend_name, ctx.target.key, {"preprocess": preprocess, "translate": translate}
    )
    ok = False
    try:
        # 1. sources
        tus = list_sources(ws.repo_root, cfg.sources, compiler=ctx.target.compiler)

        # 2. preprocess
        pp_errors: dict[str, str] = {}
        if preprocess:
            pp_errors = preprocess_all(ws, tus, timeout=cfg.budget.checker_timeout_seconds)
            if pp_errors and not quiet:
                console.print(
                    f"[yellow]{len(pp_errors)} file(s) could not be read[/] (a header or macro "
                    "is missing; `fver status <file>` shows the error)."
                )

        # 3. functions
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

        # 4. ledger + index
        ledger.upsert_tus(tus)
        ledger.upsert_functions(functions)
        removed = ledger.prune({t.id for t in tus}, {f.id for f in functions})
        if removed:
            log.info("pruned %d function(s) no longer present in the build", removed)
        index = {
            "run_id": run_id,
            "fingerprint": sources_fingerprint(ws),
            "target": ctx.target.key,
            "tus": tus,
            "preprocess_errors": pp_errors,
            "functions": functions,
            "callgraph": cg.edges,
            "externals": cg.externals,
        }

        # 5. backend front-end
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
            # A caller cannot be checked without its callee's contract, and an
            # unsupported callee will never have one: the callers are unsupported too.
            by_id = {f.id: f for f in functions}
            changed = True
            while changed:
                changed = False
                for f in functions:
                    if f.id in unsupported:
                        continue
                    bad = [by_id[c].name for c in cg.edges.get(f.id, []) if c in unsupported]
                    if bad:
                        reason = f"calls {bad[0]}, which is unsupported"
                        unsupported[f.id] = reason
                        ledger.mark_unsupported(f.id, backend_name, ctx.target.key, reason, run_id)
                        changed = True
            index["unsupported"] = unsupported
            index["tu_errors"] = tu_errors
        ws.write_state("index", index)

        # 6. proofs whose inputs changed since they were made
        stale = []
        if ctx.backend is not None:
            from fver.prove.invalidate import reconcile

            stale = reconcile(ctx, run_id)
            index["stale"] = [s.function.id for s in stale]
            ws.write_state("index", index)

        if not quiet:
            n_ok = len(functions) - len(unsupported)
            console.print(
                f"Indexed {len(tus)} file(s), {len(functions)} function(s)"
                + (f", {n_ok} accepted by the prover" if translate and ctx.backend else "")
                + (f", {len(stale)} proof(s) now stale" if stale else "")
                + "."
            )
        ok = True
        return index
    finally:
        ledger.end_run(run_id, ok, "" if ok else "scan failed")
