"""fver command-line entry point.

fver init      create .fver/ in the current repository
fver doctor    check external tools and API credentials
fver scan      capture the build, index functions, run the backend front-end
fver hunt      run bug finders (CBMC, Cerberus, sanitizers) over the code
fver verify    run the LLM proof loop over unverified functions
fver status    summary of what is proven
fver show      details for one function
fver report    write a full report (markdown / json)
fver clean     remove derived state (never user files)
fver config    get / set configuration values
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
    "fver.commands.init",
    "fver.commands.doctor",
    "fver.commands.scan",
    "fver.commands.hunt",
    "fver.commands.verify",
    "fver.commands.status",
    "fver.commands.show",
    "fver.commands.report",
    "fver.commands.clean",
    "fver.commands.config_cmd",
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
