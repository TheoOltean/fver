"""fver command-line entry point.

fver setup     install the proof toolchain
fver init      create .fver/ in the current repository
fver prove     prove the repository, a file or a function free of undefined behaviour
fver status    what is proven, what is not, and why
fver check     prove a function with your own annotations (a coding agent, a script, you)
"""

from __future__ import annotations

import importlib

import typer

from fver import __version__

app = typer.Typer(
    name="fver",
    help="Prove C code free of undefined behaviour, without touching it.",
    no_args_is_help=True,
    rich_markup_mode="rich",
)

_COMMAND_MODULES = [
    "fver.commands.setup",
    "fver.commands.init",
    "fver.commands.prove",
    "fver.commands.status",
    "fver.commands.check",
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


for _modname in _COMMAND_MODULES:
    importlib.import_module(_modname).register(app)


def main() -> None:
    app()


if __name__ == "__main__":
    main()
