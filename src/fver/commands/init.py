"""fver init: create .fver/ in the current repository."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.panel import Panel

from fver.build.detect import detect_build
from fver.commands.docs import render_docs
from fver.core.config import (
    CONFIG_DIR_NAME,
    CONFIG_FILE_NAME,
    FverConfig,
    ProjectConfig,
    TargetConfig,
    save_config,
)
from fver.core.workspace import Workspace
from fver.util.log import console


def detect_target_config() -> TargetConfig:
    compiler: str | None = None
    try:
        from fver.build.targets import detect_target
        from fver.util.platform import find_compiler

        compiler = find_compiler()
        t = detect_target(compiler) if compiler else None
    except Exception:  # module missing or compiler probe failed  # noqa: BLE001
        t = None
    if t is None:
        return TargetConfig(compiler=compiler or "cc")
    return TargetConfig(
        triple=t.triple,
        compiler=t.compiler,
        int_bits=t.int_bits,
        long_bits=t.long_bits,
        pointer_bits=t.pointer_bits,
        char_signed=t.char_signed,
        little_endian=t.little_endian,
    )


def run_init(repo_root: Path, backend: str, name: str | None, force: bool) -> Path:
    if not repo_root.is_dir():
        raise typer.BadParameter(f"{repo_root} is not a directory")
    cfg_path = repo_root / CONFIG_DIR_NAME / CONFIG_FILE_NAME
    if cfg_path.exists() and not force:
        console.print(f"[yellow]{cfg_path} already exists.[/] Use --force to overwrite the config.")
        raise typer.Exit(code=1)
    _build, notes = detect_build(repo_root)  # shown to the user; scan re-detects, nothing stored
    cfg = FverConfig(
        project=ProjectConfig(name=name or repo_root.name, backend=backend),
        target=detect_target_config(),
    )
    ws = Workspace.create(repo_root, cfg)
    save_config(repo_root, cfg)
    # Documentation for whoever (or whatever) works here next: layout, every
    # command and option, every config key, the proving workflow.
    (ws.root / "GUIDE.md").write_text(render_docs(), encoding="utf-8")
    console.print(Panel.fit(f"Initialised [bold]{ws.root}[/]", title="fver init"))
    console.print(f"  • proof stack: RefinedC on Rocq (target {cfg.target.triple})")
    for n in notes:
        console.print(f"  • {n}")
    console.print("  • settings: .fver/config.toml (commented); everything else: .fver/GUIDE.md")
    console.print(
        "\nNext steps:\n"
        "  fver scan     capture the build and index functions\n"
        "  fver hunt     run the bug finders\n"
        "  fver verify   start proving"
    )
    return cfg_path


def register(app: typer.Typer) -> None:
    @app.command("init")
    def init(
        backend: str = typer.Option("refinedc", "--backend", "-b", hidden=True),
        name: str | None = typer.Option(
            None, "--name", help="Project name (default: directory name)."
        ),
        force: bool = typer.Option(False, "--force", help="Overwrite an existing config.toml."),
        path: Path = typer.Option(
            Path("."), "--path", help="Repository root (default: cwd).", hidden=True
        ),  # noqa: B008
    ) -> None:
        """Create .fver/ in this repository."""
        run_init(path.resolve(), backend, name, force)
