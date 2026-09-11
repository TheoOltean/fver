"""`fver mcp`: run the MCP server on stdio."""

from __future__ import annotations

import typer

from fver.util.log import err_console


def register(app: typer.Typer) -> None:
    @app.command("mcp")
    def mcp() -> None:
        """Serve the fver agent protocol to Claude Code (or any MCP client) over stdio.

        Register once with: claude mcp add fver -- fver mcp
        """
        try:
            from fver.mcp.server import main
        except ImportError as e:
            err_console.print(f"[red]{e}[/]")
            raise typer.Exit(code=2) from None
        main()
