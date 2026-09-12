"""`fver show`: everything the ledger knows about one function."""

from __future__ import annotations

import typer
from rich.table import Table

from fver.commands.status import styled_status
from fver.core.context import AppContext
from fver.core.models import FunctionInfo
from fver.util.log import console, err_console


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


def print_function(ident: str, file: str | None = None) -> None:
    """Plain-text details for one function (used by `fver status -f NAME` off a terminal)."""
    _show(ident, file, False)


def register(app: typer.Typer) -> None:
    @app.command("show", hidden=True)
    def show(
        ident: str = typer.Argument(..., help="Function name or ledger id (tu_id:name)."),
        file: str | None = typer.Option(
            None, "--file", "-f", help="Repo-relative source path, to disambiguate."
        ),
        as_json: bool = typer.Option(False, "--json", help="Emit one JSON object."),
    ) -> None:
        """Details for one function: status, claim history, assumptions, proofs, findings, callers."""
        _show(ident, file, as_json)


def _show(ident: str, file: str | None, as_json: bool) -> None:
    if True:
        ctx = AppContext.load(need_backend=False)
        try:
            if as_json:
                import json

                from fver.agent import protocol

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
