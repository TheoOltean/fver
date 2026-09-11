"""fver clean: remove derived state. Never touches user files."""

from __future__ import annotations

import shutil
from pathlib import Path

import typer

from fver.core.workspace import Workspace
from fver.util.log import console

DERIVED = ("work", "backend", "cache", "logs")
PRECIOUS = ("ledger.sqlite", "proofs")


def _remove(ws: Workspace, path: Path) -> None:
    ws.assert_not_user_file(path)
    if path.is_dir():
        shutil.rmtree(path)
    elif path.exists():
        path.unlink()


def run_clean(ws: Workspace, all_: bool, yes: bool) -> list[Path]:
    removed: list[Path] = []
    for name in DERIVED:
        p = ws.root / name
        if p.exists():
            _remove(ws, p)
            removed.append(p)
    if all_:
        if not yes and not typer.confirm(
            "Also delete the ledger and all accepted proofs?", default=False
        ):
            console.print("Kept ledger.sqlite and proofs/.")
        else:
            for name in PRECIOUS:
                p = ws.root / name
                if p.exists():
                    _remove(ws, p)
                    removed.append(p)
    for p in removed:
        console.print(f"removed {p}")
    if not removed:
        console.print("Nothing to remove.")
    return removed


def register(app: typer.Typer) -> None:
    @app.command("clean")
    def clean(
        all_: bool = typer.Option(
            False, "--all", help="Also remove the ledger and accepted proofs."
        ),
        yes: bool = typer.Option(False, "--yes", "-y", help="Do not ask for confirmation."),
    ) -> None:
        """Remove derived state under .fver/ (work, backend, cache, logs)."""
        ws = Workspace.open()
        run_clean(ws, all_, yes)
