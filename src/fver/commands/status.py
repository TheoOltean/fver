"""`fver status`: summary of what is proven."""

from __future__ import annotations

import json
from dataclasses import asdict

import typer
from rich.panel import Panel
from rich.table import Table

from fver.core.context import AppContext
from fver.core.models import Status
from fver.util.log import console

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
        limit: int = typer.Option(20, "--limit", "-n", help="Functions to list (by attack score)."),
        as_json: bool = typer.Option(False, "--json", help="Print the summary as JSON."),
    ) -> None:
        """Summary of what is proven, what is not, and what it cost."""
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
