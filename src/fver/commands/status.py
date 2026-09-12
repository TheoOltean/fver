"""`fver status`: summary of what is proven."""

from __future__ import annotations

import json
import sys
from dataclasses import asdict

import typer
from rich.panel import Panel
from rich.table import Table

from fver.core.context import AppContext
from fver.core.models import FunctionInfo, Status
from fver.core.workspace import Workspace
from fver.ledger.report import build_report, to_markdown
from fver.util.log import console, err_console

STATUS_STYLE = {
    Status.VERIFIED.value: "green",
    Status.BUG_FOUND.value: "red",
    Status.UNRESOLVED.value: "yellow",
    Status.UNSUPPORTED.value: "dim",
    Status.IN_PROGRESS.value: "cyan",
    Status.NOT_ATTEMPTED.value: "white",
    "stale": "magenta",
}


def styled_status(status: str) -> str:
    return f"[{STATUS_STYLE.get(status, 'white')}]{status}[/]"


def register(app: typer.Typer) -> None:
    @app.command("status")
    def status(
        function: str | None = typer.Option(
            None, "--function", "-f", help="Open on (or, off a terminal, print) one function."
        ),
        limit: int = typer.Option(20, "--limit", "-n", help="Functions to list (by attack score)."),
        as_json: bool = typer.Option(False, "--json", help="Print the summary as JSON."),
        plain: bool = typer.Option(False, "--plain", help="Print a table instead of the view."),
        markdown: bool = typer.Option(
            False, "--markdown", help="Print the full report as markdown (for CI or sharing)."
        ),
    ) -> None:
        """What is proven, what is not, and why: a browsable view on a terminal, a table otherwise."""
        if not as_json and not plain and not markdown and sys.stdout.isatty():
            from fver.tui import run_status

            ws = Workspace.open()
            run_status(ws.repo_root, select=function)
            return
        if function:
            _show(function, None, as_json)
            return
        if markdown:
            ctx = AppContext.load(need_backend=False)
            try:
                typer.echo(
                    to_markdown(build_report(ctx.ledger, ctx.backend_name, ctx.target.key, ctx.ws))
                )
            finally:
                ctx.close()
            return
        ctx = AppContext.load(need_backend=False)
        try:
            backend = ctx.backend_name
            target_key = ctx.target.key
            summary = ctx.ledger.summary(backend, target_key)
            if as_json:
                typer.echo(json.dumps(asdict(summary), indent=2, sort_keys=True))
                return
            c = summary.total_cost
            lines = [
                f"[bold]{ctx.ws.project_name}[/]  backend=[cyan]{backend}[/]  target=[cyan]{ctx.target.triple}[/]",
                "",
                "  ".join(
                    f"{styled_status(st)}: {summary.by_status.get(st, 0)}"
                    for st in [s.value for s in Status]
                ),
                "",
                (
                    f"Functions: {summary.total_functions}   "
                    f"Attack-weighted coverage: [bold]{summary.verified_weighted * 100:.1f}%[/]   "
                    f"Findings: {summary.findings}"
                ),
                (
                    f"Cost: ${c.usd:.2f}  ({c.llm_calls} LLM calls, "
                    f"{c.input_tokens + c.cache_read_tokens + c.cache_write_tokens:,} in / "
                    f"{c.output_tokens:,} out tokens, {c.checker_runs} checker runs)"
                ),
            ]
            console.print(Panel("\n".join(lines), title="fver status", expand=False))

            rows = ctx.ledger.list_functions(backend, target_key, limit=limit)
            if not rows:
                console.print("No functions indexed yet. Run [bold]fver scan[/].")
                return
            table = Table(title=f"Top {len(rows)} functions by attack score")
            table.add_column("Function", style="bold")
            table.add_column("Location")
            table.add_column("Score", justify="right")
            table.add_column("Status")
            table.add_column("Note", overflow="fold", max_width=60)
            for r in rows:
                table.add_row(
                    r.function.name,
                    f"{r.function.source_path}:{r.function.start_line}",
                    f"{r.function.attack_score:.2f}",
                    styled_status(r.status.value),
                    (r.claim.message if r.claim else "")[:120],
                )
            console.print(table)
        finally:
            ctx.close()


def _resolve(ctx: AppContext, ident: str, file: str | None) -> FunctionInfo | None:
    fn = ctx.ledger.get_function(ident)
    if fn is not None:
        return fn
    matches = ctx.ledger.find_functions(name=ident, source_path=file)
    if not matches:
        err_console.print(
            f"[red]No function named '{ident}'[/]" + (f" in {file}" if file else "") + "."
        )
        return None
    if len(matches) > 1:
        err_console.print(f"[yellow]'{ident}' is ambiguous; pass --file to pick one:[/]")
        for m in matches:
            err_console.print(f"  {m.source_path}:{m.start_line}  ({m.id})")
        return None
    return matches[0]


def _show(ident: str, file: str | None, as_json: bool) -> None:
    if True:
        ctx = AppContext.load(need_backend=False)
        try:
            if as_json:
                import json

                from fver.prove import protocol

                try:
                    typer.echo(
                        json.dumps(
                            protocol.show(ctx, ident, file), indent=2, sort_keys=True, default=str
                        )
                    )
                except protocol.ProtocolError as e:
                    err_console.print(f"[red]{e}[/]")
                    raise typer.Exit(code=1) from None
                return
            fn = _resolve(ctx, ident, file)
            if fn is None:
                raise typer.Exit(code=1)
            backend, target_key = ctx.backend_name, ctx.target.key
            claim = ctx.ledger.current_claim(fn.id, backend, target_key)
            status = claim.status.value if claim else "not_attempted"

            console.print(f"[bold]{fn.name}[/]  {fn.source_path}:{fn.start_line}-{fn.end_line}")
            console.print(f"  id: {fn.id}    tu: {fn.tu_id}    static: {fn.is_static}")
            console.print(f"  signature: {fn.signature}")
            console.print(f"  body hash: {fn.body_hash[:16]}")
            console.print(
                f"  attack score: {fn.attack_score:.2f}"
                + (f"  ({', '.join(fn.attack_reasons)})" if fn.attack_reasons else "")
            )
            console.print(f"  callees: {', '.join(fn.callees) if fn.callees else '-'}")
            console.print(
                f"  status: {styled_status(status)}"
                + (f"  {claim.message}" if claim and claim.message else "")
            )

            history = ctx.ledger.claims_for(fn.id)
            if history:
                t = Table(title="Claim history")
                t.add_column("When")
                t.add_column("Backend")
                t.add_column("Status")
                t.add_column("Cost", justify="right")
                t.add_column("Message", overflow="fold", max_width=60)
                for c in history:
                    t.add_row(
                        c.created_at,
                        c.backend,
                        styled_status(c.status.value),
                        f"${c.cost.usd:.2f}",
                        c.message[:200],
                    )
                console.print(t)

            if claim and claim.assumptions:
                console.print("[bold]Assumptions[/] (trusted specs / axioms this proof relies on):")
                for a in claim.assumptions:
                    console.print(f"  - {a}")
            if claim and claim.proof_hash:
                console.print(f"  proof hash: {claim.proof_hash}")

            pdir = ctx.ws.proofs_dir / fn.source_path / fn.name
            if pdir.exists() and any(pdir.iterdir()):
                console.print(f"[bold]Proof directory[/]: {pdir}")
                for p in sorted(pdir.rglob("*")):
                    if p.is_file():
                        console.print(f"  {p.relative_to(pdir)}  ({p.stat().st_size} bytes)")
            else:
                console.print(f"[dim]No stored proof at {pdir}[/]")

            findings = ctx.ledger.findings(function_id=fn.id)
            if findings:
                t = Table(title="Findings")
                t.add_column("Line")
                t.add_column("Kind")
                t.add_column("Tool")
                t.add_column("Message", overflow="fold", max_width=60)
                for f in findings:
                    t.add_row(str(f.line or ""), f.kind, f.tool, f.message)
                console.print(t)

            deps = ctx.ledger.dependents(fn.name)
            if deps:
                console.print(
                    "[bold]Called by[/] (their proofs depend on this function's contract):"
                )
                for d in deps:
                    dc = ctx.ledger.current_claim(d.id, backend, target_key)
                    console.print(
                        f"  {d.name}  {d.source_path}:{d.start_line}  {styled_status(dc.status.value if dc else 'not_attempted')}"
                    )
        finally:
            ctx.close()
