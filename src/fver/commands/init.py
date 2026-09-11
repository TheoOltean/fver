"""fver init: create .fver/ in the current repository."""

from __future__ import annotations

from pathlib import Path

import typer
from rich.panel import Panel

from fver.core.config import (
    CONFIG_DIR_NAME,
    CONFIG_FILE_NAME,
    BuildConfig,
    FverConfig,
    ProjectConfig,
    TargetConfig,
    save_config,
)
from fver.core.workspace import Workspace
from fver.util.log import console

WORKSPACE_README = """# .fver/

State written by `fver`. Your source files are never modified.

| Path | What | Commit it? |
|---|---|---|
| `config.toml` | project configuration | yes |
| `ledger.sqlite` | what is proven, what is not, and why | yes (recommended) |
| `proofs/` | accepted annotations and proofs, mirroring the source tree | yes |
| `external/` | trusted specs for libc and other external functions | yes |
| `work/` | build capture, preprocessed files, function index | no (gitignored) |
| `backend/` | the proof backend's private project | no |
| `cache/` | content-addressed results | no |
| `logs/` | run logs and LLM transcripts | no |

Workflow: `fver doctor` -> `fver scan` -> `fver hunt` -> `fver verify` -> `fver status`.
"""


def detect_build(repo_root: Path) -> tuple[BuildConfig, list[str]]:
    """Guess build settings from files in the repo. Returns (config, notes)."""
    notes: list[str] = []
    build = BuildConfig()
    for cand in ("compile_commands.json", "build/compile_commands.json"):
        if (repo_root / cand).exists():
            build.compile_commands = cand
            notes.append(f"found {cand}; will use it")
            return build, notes
    if (repo_root / "CMakeLists.txt").exists():
        build.compile_commands = "build/compile_commands.json"
        notes.append(
            "CMake project: run `cmake -DCMAKE_EXPORT_COMPILE_COMMANDS=ON -B build` to produce "
            "build/compile_commands.json before `fver scan`"
        )
    elif (repo_root / "meson.build").exists():
        build.compile_commands = "build/compile_commands.json"
        notes.append(
            "Meson project: run `meson setup build` to produce build/compile_commands.json"
        )
    elif any((repo_root / m).exists() for m in ("Makefile", "makefile", "GNUmakefile")):
        build.capture_command = "bear -- make"
        notes.append(
            "Makefile project: `fver scan` will run `bear -- make` to capture the build (needs bear)"
        )
    else:
        notes.append(
            "no build system detected; every included .c file will be compiled with build.fallback_flags"
        )
    return build, notes


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
    build, notes = detect_build(repo_root)
    cfg = FverConfig(
        project=ProjectConfig(name=name or repo_root.name, backend=backend),
        build=build,
        target=detect_target_config(),
    )
    ws = Workspace.create(repo_root, cfg)
    save_config(repo_root, cfg)
    (ws.root / "README.md").write_text(WORKSPACE_README, encoding="utf-8")
    console.print(Panel.fit(f"Initialised [bold]{ws.root}[/]", title="fver init"))
    for n in notes:
        console.print(f"  • {n}")
    console.print(
        "\nNext steps:\n"
        "  fver doctor   check tools and credentials\n"
        "  fver scan     capture the build and index functions\n"
        "  fver hunt     run bug finders\n"
        "  fver verify   start proving"
    )
    return cfg_path


def register(app: typer.Typer) -> None:
    @app.command("init")
    def init(
        backend: str = typer.Option("refinedc", "--backend", "-b", help="Proof backend to use."),
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
