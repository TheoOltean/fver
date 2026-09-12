"""fver command-line entry point.

fver setup     install every external tool
fver init      create .fver/ in the current repository (rerun to refresh GUIDE.md)
fver prove     index if needed, CBMC first, then prove: the repository, a file or a function
fver status    what is proven, what is not, and why (a view on a terminal)
fver config    get / set configuration values
fver mcp       serve the session-mode protocol to Claude Code
fver agent     the same protocol as plain commands (hidden)
"""

from __future__ import annotations

import importlib

import typer

from fver import __version__

app = typer.Typer(
    name="fver",
    help="LLM-driven formal verification of C: prove absence of undefined behaviour.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

_COMMAND_MODULES = [
    "fver.commands.setup",
    "fver.commands.init",
    "fver.commands.prove",
    "fver.commands.status",
    "fver.commands.config_cmd",
    "fver.commands.agent_cmds",
    "fver.commands.mcp_cmd",
]


def _version_callback(value: bool) -> None:
    if value:
        typer.echo(f"fver {__version__}")
        raise typer.Exit()


@app.callback()
def _root(
    version: bool = typer.Option(
        False, "--version", "-V", callback=_version_callback, is_eager=True, help="Show version."
    ),
) -> None:
    pass


def _register_all() -> None:
    for modname in _COMMAND_MODULES:
        try:
            mod = importlib.import_module(modname)
        except ModuleNotFoundError as e:  # a command not yet implemented
            if e.name == modname:
                continue
            raise
        register = getattr(mod, "register", None)
        if register is not None:
            register(app)


_register_all()


def main() -> None:
    app()


if __name__ == "__main__":
    main()
