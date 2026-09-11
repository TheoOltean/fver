"""`fver report`: write a full markdown or JSON report."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import typer

from fver.core.context import AppContext
from fver.ledger.report import build_report, to_json, to_markdown
from fver.util.log import console


def register(app: typer.Typer) -> None:
    @app.command("report")
    def report(
        fmt: str = typer.Option("md", "--format", "-f", help="md or json."),
        out: str | None = typer.Option(None, "--out", "-o", help="Output path, or '-' for stdout."),
        allow_outside: bool = typer.Option(
            False, "--allow-outside", help="Allow --out outside .fver/."
        ),
    ) -> None:
        """Write a full report of what is proven, unresolved, and assumed."""
        if fmt not in ("md", "json"):
            raise typer.BadParameter("--format must be md or json")
        ctx = AppContext.load(need_backend=False)
        try:
            data = build_report(ctx.ledger, ctx.backend_name, ctx.target.key, ctx.ws)
            text = to_markdown(data) if fmt == "md" else to_json(data)
            if out == "-":
                typer.echo(text)
                return
            if out is None:
                stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
                path = ctx.ws.root / "reports" / f"{stamp}-report.{fmt}"
            else:
                path = Path(out)
                if not allow_outside:
                    ctx.ws.assert_not_user_file(path)
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(text, encoding="utf-8")
            console.print(f"Report written to {path}")
        finally:
            ctx.close()
