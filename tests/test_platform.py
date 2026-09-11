"""Portability helpers: install hints per package manager, compiler/triple fallbacks."""

from __future__ import annotations

from fver.build import targets
from fver.util import platform as plat
from fver.util.proc import ProcResult


def _reset():
    plat.package_manager.cache_clear()
    plat.find_compiler.cache_clear()
    plat.compiler_supports.cache_clear()


def test_install_hint_per_manager(monkeypatch):
    _reset()
    monkeypatch.setattr(plat, "which", lambda n: "/usr/bin/apt-get" if n == "apt-get" else None)
    plat.package_manager.cache_clear()
    assert plat.install_hint("cbmc") == "sudo apt-get install -y cbmc"
    assert plat.install_hint({"brew": "x", "*": "y"}, url="https://u") == (
        "sudo apt-get install -y y | https://u"
    )
    _reset()
    monkeypatch.setattr(plat, "which", lambda n: "/opt/homebrew/bin/brew" if n == "brew" else None)
    plat.package_manager.cache_clear()
    assert plat.install_hint({"brew": "llvm", "*": "clang"}) == "brew install llvm"
    _reset()
    monkeypatch.setattr(plat, "which", lambda n: None)
    plat.package_manager.cache_clear()
    assert plat.install_hint("cbmc", url="https://u") == "https://u"
    assert plat.install_hint("cbmc") == "install it with your package manager"
    _reset()


def test_find_compiler_order(monkeypatch):
    _reset()
    monkeypatch.setattr(plat, "which", lambda n: "/usr/bin/gcc" if n == "gcc" else None)
    assert plat.find_compiler() == "gcc"
    _reset()
    monkeypatch.setattr(plat, "which", lambda n: None)
    assert plat.find_compiler() is None
    _reset()


def _fake_run(responses):
    def run(argv, **kw):
        for key, (code, out) in responses.items():
            if key in argv:
                return ProcResult(argv, code, out, "", 0.0)
        return ProcResult(argv, 1, "", "unknown option", 0.0)

    return run


def test_detect_triple_prefers_clang_then_gcc(monkeypatch):
    monkeypatch.setattr(
        targets.proc, "run", _fake_run({"-print-target-triple": (0, "arm64-apple-darwin\n")})
    )
    assert targets.detect_triple("clang") == "arm64-apple-darwin"
    monkeypatch.setattr(targets.proc, "run", _fake_run({"-dumpmachine": (0, "x86_64-linux-gnu\n")}))
    assert targets.detect_triple("gcc") == "x86_64-linux-gnu"
    monkeypatch.setattr(targets.proc, "run", _fake_run({}))
    assert targets.detect_triple("tcc") == "unknown"


def test_detect_target_survives_all_probes_failing(monkeypatch):
    monkeypatch.setattr(targets.proc, "which", lambda n: "/usr/bin/cc")
    monkeypatch.setattr(targets.proc, "run", _fake_run({}))
    t = targets.detect_target("cc")
    assert t is not None and t.triple == "unknown" and t.compiler == "cc"
    assert targets.compare_target(t, t) == []
