"""`fver setup`: the plan is deterministic and complete; nothing is optional."""

from __future__ import annotations

from typer.testing import CliRunner

from fver.cli import app
from fver.commands import setup
from fver.util import platform as plat


def test_system_step_per_package_manager(monkeypatch):
    plat.package_manager.cache_clear()
    monkeypatch.setattr(plat, "package_manager", lambda: ("brew", "brew install"))
    step = setup.system_step()
    assert step is not None and step.argv[:2] == ["brew", "install"]
    for pkg in ("opam", "cbmc"):
        assert pkg in step.argv
    monkeypatch.setattr(plat, "package_manager", lambda: ("apt-get", "sudo apt-get install -y"))
    step = setup.system_step()
    assert step is not None and step.argv[:4] == ["sudo", "apt-get", "install", "-y"]
    monkeypatch.setattr(plat, "package_manager", lambda: ("pkg_add", "doas pkg_add"))
    assert setup.system_step() is None
    monkeypatch.setattr(plat, "package_manager", lambda: None)
    assert setup.system_step() is None


def test_opam_steps_pin_the_calibrated_commits(monkeypatch):
    monkeypatch.setattr(setup, "_opam_root_exists", lambda: True)
    monkeypatch.setattr(setup, "_switch_exists", lambda: False)
    steps = setup.opam_steps(jobs=3)
    titles = [s.title for s in steps]
    assert titles[0].startswith("initialise opam") and steps[0].skip_if
    assert "create opam switch" in titles[1] and not steps[1].skip_if
    flat = " ".join(" ".join(s.argv) for s in steps)
    assert setup.CERBERUS_PIN in flat and setup.REFINEDC_PIN in flat
    assert "coq-released" in flat and "iris-dev" in flat
    assert steps[-1].argv[-1] == "refinedc" and steps[-1].env["OPAMJOBS"] == "3"
    assert all("--switch" in s.argv for s in steps[2:])


def test_setup_refuses_without_package_manager(monkeypatch):
    monkeypatch.setattr(setup.shutil, "which", lambda n: "/usr/bin/cc" if n == "cc" else None)
    monkeypatch.setattr(setup, "system_step", lambda: None)
    r = CliRunner().invoke(app, ["setup"])
    assert r.exit_code == 1 and "package manager" in r.output


def test_setup_is_a_documented_command():
    r = CliRunner().invoke(app, ["setup", "--help"])
    assert r.exit_code == 0 and "RefinedC" in r.output
