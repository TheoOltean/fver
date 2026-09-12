"""`fver setup`: install every external tool fver needs. No optional tools.

- a C compiler (checked, not installed: it comes with the platform's
  developer tooling)
- cbmc, the bounded model checker used by `fver hunt`
- an opam switch named `fver` holding Rocq, Iris, Cerberus and RefinedC,
  pinned to the commits fver is calibrated against

Package installs go through the platform's package manager. The opam part
follows RefinedC's README and takes 20 to 40 minutes the first time; every
step is idempotent, so rerunning after a failure resumes. The absolute paths
of the installed binaries are written to the user-level config, so nothing
has to be added to PATH.
"""

from __future__ import annotations

import os
import shutil
import subprocess
from dataclasses import dataclass, field
from pathlib import Path

import typer

from fver.util import platform as plat
from fver.util.log import console, err_console

SWITCH = "fver"
OCAML = "ocaml-base-compiler.4.14.2"
REPOS = {
    "coq-released": "https://coq.inria.fr/opam/released",
    "iris-dev": "git+https://gitlab.mpi-sws.org/iris/opam.git",
}
# CONFIRMED working combination (fver's own tests run against it).
CERBERUS_PIN = (
    "git+https://github.com/rems-project/cerberus.git#f11e6b335a687c1b77539f7e5695607d09dfc3ea"
)
REFINEDC_PIN = (
    "git+https://gitlab.mpi-sws.org/iris/refinedc.git#2e89846baeaa7f65cbe17e3ae7297a8ed5b4feca"
)

# Packages per package manager. Every manager listed here can install all
# of them; anything else gets a message with the URLs.
PACKAGES: dict[str, list[str]] = {
    "brew": ["opam", "cbmc", "gmp", "pkg-config"],
    "apt-get": ["opam", "cbmc", "build-essential", "m4", "libgmp-dev", "pkg-config"],
    "dnf": ["opam", "cbmc", "gcc", "make", "m4", "gmp-devel", "pkgconf-pkg-config"],
    "pacman": ["opam", "cbmc", "base-devel", "gmp"],
}
URLS = {
    "opam": "https://opam.ocaml.org/doc/Install.html",
    "cbmc": "https://github.com/diffblue/cbmc/releases",
}


@dataclass
class Step:
    title: str
    argv: list[str]
    skip_if: bool = False
    env: dict[str, str] = field(default_factory=dict)


def _opam_root_exists() -> bool:
    root = os.environ.get("OPAMROOT") or str(Path.home() / ".opam")
    return (Path(root) / "config").exists()


def _switch_exists() -> bool:
    if not shutil.which("opam"):
        return False
    r = subprocess.run(
        ["opam", "switch", "list", "--short"], capture_output=True, text=True, check=False
    )
    return r.returncode == 0 and SWITCH in r.stdout.split()


def _switch_bin() -> Path:
    root = os.environ.get("OPAMROOT") or str(Path.home() / ".opam")
    return Path(root) / SWITCH / "bin"


def system_step() -> Step | None:
    """The package-manager step for this machine, or None if unknown."""
    pm = plat.package_manager()
    if pm is None or pm[0] not in PACKAGES:
        return None
    name, prefix = pm
    return Step(f"install system packages with {name}", [*prefix.split(), *PACKAGES[name]])


def opam_steps(jobs: int) -> list[Step]:
    """The opam steps, each skipped when its result already exists."""
    sw = ["--switch", SWITCH]
    env = {"OPAMYES": "1", "OPAMJOBS": str(jobs), "OPAMCONFIRMLEVEL": "unsafe-yes"}
    steps = [
        Step(
            "initialise opam",
            ["opam", "init", "--bare", "--no-setup", "-y"],
            skip_if=_opam_root_exists(),
            env=env,
        ),
        Step(
            f"create opam switch `{SWITCH}` ({OCAML})",
            ["opam", "switch", "create", SWITCH, OCAML],
            skip_if=_switch_exists(),
            env=env,
        ),
    ]
    for rname, url in REPOS.items():
        steps.append(
            Step(f"add opam repository {rname}", ["opam", "repo", "add", *sw, rname, url], env=env)
        )
    steps += [
        Step("update opam repositories", ["opam", "update", *sw], env=env),
        Step(
            "pin cerberus-lib",
            ["opam", "pin", "add", *sw, "-n", "-y", "cerberus-lib", CERBERUS_PIN],
            env=env,
        ),
        Step(
            "pin refinedc",
            ["opam", "pin", "add", *sw, "-n", "-y", "refinedc", REFINEDC_PIN],
            env=env,
        ),
        Step(
            "build Rocq, Iris, Cerberus and RefinedC (20-40 minutes the first time)",
            ["opam", "install", *sw, "-y", "--confirm-level=unsafe-yes", "refinedc"],
            env=env,
        ),
    ]
    return steps


def _run(step: Step) -> None:
    console.print(f"[bold]==>[/] {step.title}")
    console.print(f"    $ {' '.join(step.argv)}", style="dim")
    env = {**os.environ, **step.env}
    r = subprocess.run(step.argv, env=env, check=False)
    if r.returncode != 0:
        # `opam repo add` of an existing repository is the one benign failure.
        if step.argv[:3] == ["opam", "repo", "add"]:
            return
        raise typer.Exit(code=r.returncode or 1)


def record_tool_paths() -> None:
    """Point the user-level config at the switch's binaries."""
    from fver.commands.config_cmd import set_user_value

    b = _switch_bin()
    for key, name in (("refinedc_bin", "refinedc"), ("coqc_bin", "coqc"), ("dune_bin", "dune")):
        set_user_value(f"backend.refinedc.{key}", f'"{b / name}"')


def run_setup(jobs: int) -> None:
    if not shutil.which("cc") and not shutil.which("clang") and not shutil.which("gcc"):
        err_console.print(
            "[red]No C compiler found.[/] Install your platform's developer tools first "
            "(macOS: `xcode-select --install`; Debian/Ubuntu: `sudo apt-get install "
            "build-essential`), then rerun `fver setup`."
        )
        raise typer.Exit(code=1)
    sys_step = system_step()
    if sys_step is None:
        err_console.print(
            "[red]No supported package manager found[/] (brew, apt-get, dnf or pacman). "
            "Install these yourself, then rerun `fver setup`:\n"
            + "\n".join(f"  {k}: {v}" for k, v in URLS.items())
        )
        raise typer.Exit(code=1)
    _run(sys_step)
    for step in opam_steps(jobs):
        if step.skip_if:
            console.print(f"[bold]==>[/] {step.title}: already done", style="dim")
            continue
        _run(step)
    record_tool_paths()
    console.print(f"\n[green]Toolchain installed[/] in {_switch_bin().parent}.")
    from fver.core.doctor import collect_statuses, render

    rows, _ = collect_statuses()
    code = render(rows)
    if code:
        raise typer.Exit(code=code)


def register(app: typer.Typer) -> None:
    @app.command("setup")
    def setup(
        jobs: int = typer.Option(
            max(2, (os.cpu_count() or 4) - 1), "--jobs", "-j", help="Parallel opam build jobs."
        ),
    ) -> None:
        """Install every tool fver needs: cbmc, and an opam switch with Rocq, Iris, Cerberus and RefinedC. Idempotent; rerun to resume."""
        run_setup(jobs)
