"""`fver mcp`: run the MCP server on stdio."""

from __future__ import annotations

import typer


def register(app: typer.Typer) -> None:
    @app.command("mcp")
    def mcp() -> None:
        """Serve the fver agent protocol to Claude Code (or any MCP client) over stdio.

        Register once with: claude mcp add fver -- fver mcp
        """
        from fver.mcp.server import main

        main()
