"""fver init: create .fver/ in the current repository."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.panel import Panel

from fver.core.config import (
    CONFIG_DIR_NAME,
    CONFIG_FILE_NAME,
    FverConfig,
    ProjectConfig,
    save_config,
)
from fver.core.guide import render_docs
from fver.core.workspace import Workspace
from fver.index.detect import detect_build
from fver.util.log import console


def run_init(repo_root: Path, backend: str, name: str | None, force: bool) -> Path:
    if not repo_root.is_dir():
        raise typer.BadParameter(f"{repo_root} is not a directory")
    cfg_path = repo_root / CONFIG_DIR_NAME / CONFIG_FILE_NAME
    if cfg_path.exists() and not force:
        # Already initialised: refresh the documentation, leave the config alone.
        ws = Workspace.open(repo_root)
        (ws.root / "GUIDE.md").write_text(render_docs(), encoding="utf-8")
        console.print(f"Already initialised: {ws.root}. Refreshed GUIDE.md; config untouched.")
        return cfg_path
    _build, notes = detect_build(repo_root)  # shown to the user; scan re-detects, nothing stored
    cfg = FverConfig(project=ProjectConfig(name=name, backend=backend))
    ws = Workspace.create(repo_root, cfg)
    save_config(repo_root, cfg)
    from fver.core.context import resolve_target

    target = resolve_target(ws, cfg, redetect=True)
    # Documentation for whoever (or whatever) works here next: layout, every
    # command and option, every config key, the proving workflow.
    (ws.root / "GUIDE.md").write_text(render_docs(), encoding="utf-8")
    console.print(Panel.fit(f"Initialised [bold]{ws.root}[/]", title="fver init"))
    console.print(f"  • proof stack: RefinedC on Rocq, target {target.triple}")
    console.print(
        f"  • model {cfg.model.model} at effort {cfg.model.effort}; budget "
        f"${cfg.budget.max_usd_per_run:.0f} per run, ${cfg.budget.max_usd_per_function:.0f} "
        "per function (edit .fver/config.toml)"
    )
    console.print("  • API key (API mode only): fver config set --user model.api_key sk-ant-...")
    for n in notes:
        console.print(f"  • {n}")
    console.print("  • settings: .fver/config.toml (commented); everything else: .fver/GUIDE.md")
    console.print(
        "\nNext: fver prove            # the whole repository; or a file, or a function\n"
        "      fver status           # what is proven"
    )
    return cfg_path


def register(app: typer.Typer) -> None:
    @app.command("init")
    def init(
        backend: str = typer.Option("refinedc", "--backend", "-b", hidden=True),
        name: str | None = typer.Option(
            None, "--name", help="Project name (default: directory name)."
        ),
        force: bool = typer.Option(
            False, "--force", help="Reset an existing config.toml to the defaults."
        ),
        path: Path = typer.Option(
            Path("."), "--path", help="Repository root (default: cwd).", hidden=True
        ),  # noqa: B008
    ) -> None:
        """Create .fver/ in this repository."""
        run_init(path.resolve(), backend, name, force)
