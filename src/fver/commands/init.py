"""fver init: create .fver/ in the current repository."""

from __future__ import annotations

from pathlib import Path

import typer

from fver.core.config import (
    CONFIG_DIR_NAME,
    CONFIG_FILE_NAME,
    FverConfig,
    ProjectConfig,
    save_config,
)
from fver.core.workspace import GITIGNORE_BODY, Workspace
from fver.util.log import console


def run_init(repo_root: Path, backend: str) -> Path:
    if not repo_root.is_dir():
        raise typer.BadParameter(f"{repo_root} is not a directory")
    cfg_path = repo_root / CONFIG_DIR_NAME / CONFIG_FILE_NAME
    if cfg_path.exists():
        ws = Workspace.open(repo_root)
        (ws.root / ".gitignore").write_text(GITIGNORE_BODY, encoding="utf-8")
        console.print(f"Already initialised: {ws.root}")
        return cfg_path
    cfg = FverConfig(project=ProjectConfig(backend=backend))
    ws = Workspace.create(repo_root, cfg)
    save_config(repo_root, cfg)
    from fver.core.context import resolve_target

    target = resolve_target(ws, redetect=True)
    console.print(f"Initialised {ws.root} (proofs for {target.triple}).")
    console.print(f"Put your API key in {cfg_path} (git ignores it), then:")
    console.print("  fver status    index the code; what is provable")
    console.print("  fver prove     the whole repository, or a file, or a function")
    return cfg_path


def register(app: typer.Typer) -> None:
    @app.command("init")
    def init(
        backend: str = typer.Option("framac", "--backend", "-b", hidden=True),
        path: Path = typer.Option(
            Path("."), "--path", help="Repository root (default: cwd).", hidden=True
        ),  # noqa: B008
    ) -> None:
        """Create .fver/ in this repository."""
        run_init(path.resolve(), backend)
