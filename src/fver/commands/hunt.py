"""fver hunt: run bug finders over the indexed code."""

from __future__ import annotations

import logging
from collections import defaultdict
from dataclasses import asdict

import typer
from rich.table import Table

from fver.core.models import Claim, Finding, FunctionInfo, PropertyClass, Status, TranslationUnit
from fver.hunters.base import UB_KINDS, enabled_hunters
from fver.util.log import console, setup_logging

log = logging.getLogger(__name__)


def _load_index(
    ctx, function: str | None, file: str | None
) -> tuple[list[TranslationUnit], list[FunctionInfo]]:
    rows = ctx.ledger.list_functions(ctx.backend_name, ctx.target.key, order_by_attack_score=True)
    functions = [r.function for r in rows]
    if function:
        functions = [f for f in functions if f.name == function]
    if file:
        functions = [f for f in functions if f.source_path == file]
    tus: dict[str, TranslationUnit] = {}
    for f in functions:
        if f.tu_id not in tus:
            tu = ctx.ledger.get_tu(f.tu_id)
            if tu is not None:
                tus[f.tu_id] = tu
    return list(tus.values()), functions


def claim_for_finding(
    fi: FunctionInfo, finding: Finding, backend: str, target_key: str, run_id: str
) -> Claim:
    where = f" at line {finding.line}" if finding.line is not None else ""
    return Claim(
        function_id=fi.id,
        property_class=PropertyClass.UB_FREE,
        backend=backend,
        target_key=target_key,
        status=Status.BUG_FOUND,
        body_hash=fi.body_hash,
        cache_key="",
        message=f"{finding.tool}: {finding.kind}{where}",
        run_id=run_id,
        extra={"finding": asdict(finding)},
    )


def run_hunt(
    ctx,
    only: list[str] | None,
    function: str | None,
    file: str | None,
    selected: list[FunctionInfo] | None = None,
    quiet: bool = False,
) -> list[Finding]:
    if selected is not None:
        functions = list(selected)
        tus_by_id: dict[str, TranslationUnit] = {}
        for f in functions:
            if f.tu_id not in tus_by_id:
                tu = ctx.ledger.get_tu(f.tu_id)
                if tu is not None:
                    tus_by_id[f.tu_id] = tu
        tus = list(tus_by_id.values())
    else:
        tus, functions = _load_index(ctx, function, file)
    if not functions:
        if not quiet:
            console.print("[yellow]No functions indexed.[/] Run `fver scan` first.")
        return []
    hunters = enabled_hunters(ctx.config.hunters, only)
    if not hunters:
        if not quiet:
            console.print("[yellow]No such hunter.[/] Choose from: cbmc, sanitizers.")
        return []
    by_id = {f.id: f for f in functions}
    run_id = ctx.ledger.start_run(
        "hunt", ctx.backend_name, ctx.target.key, {"hunters": [h.name for h in hunters]}
    )
    all_findings: list[Finding] = []
    ok = True
    try:
        for h in hunters:
            workdir = ctx.ws.work_dir / "hunt" / h.name
            log.info("running %s over %d translation unit(s)", h.name, len(tus))
            try:
                found = h.run(tus, functions, ctx.ws.repo_root, workdir, ctx.config.hunters)
            except Exception as e:  # one hunter failing must not lose the others  # noqa: BLE001
                ok = False
                log.error("%s failed: %s", h.name, e)
                continue
            ctx.ledger.delete_findings({t.source_path for t in tus}, tool=h.name)
            for fnd in found:
                fnd.run_id = run_id
                ctx.ledger.record_finding(fnd)
                fi = by_id.get(fnd.function_id) if fnd.function_id else None
                if fi is not None and fnd.kind in UB_KINDS and fnd.confidence == "high":
                    ctx.ledger.record_claim(
                        claim_for_finding(fi, fnd, ctx.backend_name, ctx.target.key, run_id)
                    )
            all_findings.extend(found)
        # A function this run analysed without a confirmed bug no longer
        # deserves an older hunter-issued bug_found status.
        confirmed = {
            f.function_id
            for f in all_findings
            if f.function_id and f.kind in UB_KINDS and f.confidence == "high"
        }
        for fi in functions:
            if fi.id in confirmed:
                continue
            cur = ctx.ledger.current_claim(fi.id, ctx.backend_name, ctx.target.key)
            if cur is None or cur.status is not Status.BUG_FOUND or "finding" not in cur.extra:
                continue
            ctx.ledger.record_claim(
                Claim(
                    function_id=fi.id,
                    property_class=PropertyClass.UB_FREE,
                    backend=ctx.backend_name,
                    target_key=ctx.target.key,
                    status=Status.NOT_ATTEMPTED,
                    body_hash=fi.body_hash,
                    cache_key="",
                    message="earlier hunter finding not reproduced in the latest hunt run",
                    run_id=run_id,
                    extra={"superseded": cur.message},
                )
            )
    finally:
        ctx.ledger.end_run(run_id, ok, f"{len(all_findings)} finding(s)")
    return all_findings


def render_findings(findings: list[Finding]) -> None:
    if not findings:
        console.print("[green]No findings.[/]")
        return
    by_file: dict[str, list[Finding]] = defaultdict(list)
    for f in findings:
        by_file[f.source_path].append(f)
    table = Table(title="findings")
    table.add_column("file")
    table.add_column("line", justify="right")
    table.add_column("kind")
    table.add_column("tool")
    table.add_column("message")
    for path in sorted(by_file):
        for f in sorted(by_file[path], key=lambda x: (x.line or 0, x.kind)):
            if f.kind not in UB_KINDS:
                style = "dim"
            elif f.confidence == "high":
                style = "red"
            else:
                style = "yellow"
            table.add_row(path, str(f.line or ""), f"[{style}]{f.kind}[/]", f.tool, f.message[:100])
    console.print(table)
    real = sum(1 for f in findings if f.kind in UB_KINDS and f.confidence == "high")
    possible = sum(1 for f in findings if f.kind in UB_KINDS and f.confidence != "high")
    console.print(
        f"{len(findings)} finding(s): {real} undefined behaviour reached from a real entry "
        f"point (recorded as bug_found), {possible} possible under unconstrained inputs "
        f"(recorded, not counted as bugs), {len(findings) - real - possible} informational."
    )


def register(app: typer.Typer) -> None:
    @app.command("hunt")
    def hunt(
        only: list[str] = typer.Option(
            None, "--only", help="Run only these hunters (cbmc, sanitizers)."
        ),  # noqa: B008
        function: str | None = typer.Option(
            None, "--function", "-f", help="Restrict to one function name."
        ),
        file: str | None = typer.Option(
            None, "--file", help="Restrict to one source file (repo-relative)."
        ),
        verbose: bool = typer.Option(False, "--verbose", "-v"),
    ) -> None:
        """Run the bug finders (CBMC; sanitizers when hunters.test_command is set) over the indexed code."""
        from fver.core.context import AppContext

        ctx = AppContext.load(need_backend=False)
        setup_logging(ctx.ws.logs_dir, verbose, "hunt")
        try:
            findings = run_hunt(ctx, list(only) if only else None, function, file)
            render_findings(findings)
        finally:
            ctx.close()
