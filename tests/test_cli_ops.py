from __future__ import annotations

import pathlib

import pytest
from typer.testing import CliRunner

from fver.cli import app
from fver.commands import clean as clean_cmd
from fver.commands import config_cmd, doctor, hunt
from fver.commands import docs as docs_cmd
from fver.core.config import FverConfig, HuntersConfig, load_config
from fver.core.models import Finding, FunctionInfo, Status, Target, TranslationUnit
from fver.core.workspace import Workspace
from fver.util import proc

runner = CliRunner()


@pytest.fixture
def repo(tmp_path, monkeypatch):
    d = tmp_path / "proj"
    (d / "src").mkdir(parents=True)
    (d / "src" / "a.c").write_text("int f(void){return 1;}")
    (d / "Makefile").write_text("all:\n\tcc src/a.c\n")
    monkeypatch.chdir(d)
    monkeypatch.setenv("FVER_HOME", str(tmp_path / "home"))  # ignore the user's real ~/.fver
    return d


# --- init ---------------------------------------------------------------


def test_init_creates_layout_and_detects_makefile(repo):
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0, r.output
    ws = Workspace.open(repo)
    for d in ("proofs", "external", "work", "backend", "cache", "logs"):
        assert (ws.root / d).is_dir()
    assert (ws.root / ".gitignore").exists()
    # The workspace README documents every command, every config key and the workflow.
    readme = (ws.root / "GUIDE.md").read_text()
    for needle in ("### `fver check`", "`--submission`", "[verify]", "next_limit", "## Goal"):
        assert needle in readme
    cfg = load_config(repo)
    assert cfg.project.name == "proj" and cfg.project.backend == "refinedc"
    # Nothing about the build is stored; scan detects it. The file is commented.
    assert cfg.build.capture_command is None and cfg.build.compile_commands is None
    text = (ws.root / "config.toml").read_text()
    assert text.startswith("# fver project configuration") and "[build]" not in text
    assert 'backend = "refinedc"' not in text and "# The machine the proofs are for" in text
    assert "Makefile project" in r.output
    # user tree untouched apart from .fver
    assert sorted(p.name for p in repo.iterdir()) == [".fver", "Makefile", "src"]


def test_init_refuses_overwrite_without_force(repo):
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(app, ["init"])
    assert r.exit_code == 1 and "already exists" in r.output
    r = runner.invoke(app, ["init", "--force", "--backend", "null", "--name", "other"])
    assert r.exit_code == 0
    cfg = load_config(repo)
    assert cfg.project.backend == "null" and cfg.project.name == "other"


def test_init_detects_cmake_and_existing_compile_commands(tmp_path, monkeypatch):
    d = tmp_path / "cm"
    d.mkdir()
    (d / "CMakeLists.txt").write_text("project(x C)")
    monkeypatch.chdir(d)
    monkeypatch.setenv("FVER_HOME", str(tmp_path / "home"))
    from fver.build.detect import detect_build, resolve_build
    from fver.core.config import BuildConfig

    r = runner.invoke(app, ["init"])
    assert r.exit_code == 0 and "CMake project" in r.output
    assert detect_build(d)[0].compile_commands == "build/compile_commands.json"
    (d / "compile_commands.json").write_text("[]")
    assert detect_build(d)[0].compile_commands == "compile_commands.json"
    # A configured source of flags wins over detection; include/exclude survive a detection.
    chosen = BuildConfig(capture_command="bear -- ninja", include=["lib/**/*.c"])
    assert resolve_build(d, chosen)[0] == chosen
    auto, _ = resolve_build(d, BuildConfig(include=["lib/**/*.c"]))
    assert auto.compile_commands == "compile_commands.json" and auto.include == ["lib/**/*.c"]


# --- config -------------------------------------------------------------


def test_config_get_set_roundtrip(repo):
    runner.invoke(app, ["init"])
    assert runner.invoke(app, ["config", "get", "model.effort"]).output.strip() == "high"
    r = runner.invoke(app, ["config", "set", "model.effort", "xhigh"])
    assert r.exit_code == 0, r.output
    assert load_config(repo).model.effort == "xhigh"
    assert runner.invoke(app, ["config", "set", "hunters.cbmc_unwind", "12"]).exit_code == 0
    assert load_config(repo).hunters.cbmc_unwind == 12
    assert runner.invoke(app, ["config", "set", "verify.limit", "5"]).exit_code == 0
    assert runner.invoke(app, ["config", "set", "verify.follow_callees", "false"]).exit_code == 0
    assert load_config(repo).verify.limit == 5 and load_config(repo).verify.follow_callees is False
    assert (
        runner.invoke(app, ["config", "set", "budget.max_usd_per_function", "2.5"]).exit_code == 0
    )
    assert load_config(repo).budget.max_usd_per_function == 2.5
    assert runner.invoke(app, ["config", "set", "build.include", '["src/**/*.c"]']).exit_code == 0
    assert load_config(repo).build.include == ["src/**/*.c"]
    # bare strings and backend tables
    assert (
        runner.invoke(app, ["config", "set", "hunters.test_command", "make check"]).exit_code == 0
    )
    assert load_config(repo).hunters.test_command == "make check"
    assert (
        runner.invoke(app, ["config", "set", "backend.refinedc.refinedc_bin", "/opt/rc"]).exit_code
        == 0
    )
    assert load_config(repo).backend_settings("refinedc")["refinedc_bin"] == "/opt/rc"
    # validation errors are reported, config unchanged
    r = runner.invoke(app, ["config", "set", "budget.max_attempts_per_function", "many"])
    assert r.exit_code != 0
    assert load_config(repo).budget.max_attempts_per_function == 8
    assert runner.invoke(app, ["config", "get", "nope.key"]).exit_code == 1
    assert str(repo / ".fver" / "config.toml") in runner.invoke(app, ["config", "path"]).output
    assert "[project]" in runner.invoke(app, ["config", "show"]).output


def test_config_set_never_copies_user_settings_into_the_project(repo):
    runner.invoke(app, ["init"])
    assert (
        runner.invoke(app, ["config", "set", "--user", "model.api_key", "sk-secret"]).exit_code == 0
    )
    assert (
        runner.invoke(
            app, ["config", "set", "--user", "backend.refinedc.refinedc_bin", "/opt/rc"]
        ).exit_code
        == 0
    )
    assert runner.invoke(app, ["config", "set", "budget.max_usd_per_run", "50"]).exit_code == 0
    project = (repo / ".fver" / "config.toml").read_text()
    assert "max_usd_per_run = 50" in project
    assert "sk-secret" not in project and "/opt/rc" not in project
    assert load_config(repo).model.api_key == "sk-secret"  # still effective through the merge
    assert project.count("\n\n\n") == 0  # no double blank lines


def test_docs_command_prints_and_refreshes_readme(repo):
    assert runner.invoke(app, ["init"]).exit_code == 0
    r = runner.invoke(app, ["docs"])
    assert r.exit_code == 0 and "### `fver verify`" in r.output and "[budget]" in r.output
    readme = repo / ".fver" / "GUIDE.md"
    readme.write_text("stale")
    r = runner.invoke(app, ["docs", "--write"])
    assert r.exit_code == 0 and readme.read_text() == docs_cmd.render_docs()


def test_prover_workflow_matches_claude_skill():
    """The skill shipped for Claude Code and the workflow written into .fver/GUIDE.md
    must say the same thing."""
    skill = pathlib.Path(__file__).resolve().parents[1] / ".claude" / "skills" / "fver" / "SKILL.md"
    if not skill.exists():
        pytest.skip("skill file not in this checkout")
    body = skill.read_text().split("---", 2)[2].lstrip()
    assert body == docs_cmd.PROVER_WORKFLOW


def test_parse_value():
    assert config_cmd.parse_value("true") is True
    assert config_cmd.parse_value("3") == 3
    assert config_cmd.parse_value('"x"') == "x"
    assert config_cmd.parse_value("plain words") == "plain words"


# --- clean --------------------------------------------------------------


def test_clean_removes_only_derived_state(repo):
    runner.invoke(app, ["init"])
    ws = Workspace.open(repo)
    (ws.work_dir / "x").write_text("x")
    (ws.proofs_dir / "p").write_text("p")
    ws.ledger_path.write_text("db")
    r = runner.invoke(app, ["clean"])
    assert r.exit_code == 0, r.output
    assert not ws.work_dir.exists() and not ws.cache_dir.exists()
    assert (ws.proofs_dir / "p").exists() and ws.ledger_path.exists()
    r = runner.invoke(app, ["clean", "--all", "--yes"])
    assert r.exit_code == 0
    assert not ws.proofs_dir.exists() and not ws.ledger_path.exists()
    assert (repo / "src" / "a.c").exists()


def test_clean_never_deletes_outside_workspace(repo):
    runner.invoke(app, ["init"])
    ws = Workspace.open(repo)
    with pytest.raises(PermissionError):
        clean_cmd._remove(ws, repo / "src")
    assert (repo / "src" / "a.c").exists()


# --- doctor -------------------------------------------------------------


def test_doctor_exit_codes(repo, monkeypatch):
    runner.invoke(app, ["init", "--backend", "null"])
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(proc, "which", lambda n: None)
    r = runner.invoke(app, ["doctor"])
    assert r.exit_code == 1 and "missing" in r.output
    assert "optional" not in r.output  # every tool is required; credentials are informational
    assert "not set" in r.output
    monkeypatch.setattr(proc, "which", lambda n: f"/usr/bin/{n}")
    monkeypatch.setattr(
        proc, "run", lambda argv, **kw: proc.ProcResult(argv, 0, "tool 1.0\n", "", 0.0)
    )
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-test")
    rows, _ = doctor.collect_statuses()
    # the null backend may or may not be implemented yet; ignore backend rows
    required_missing = [
        x for x in rows if x.required and not x.found and not x.name.startswith("backend")
    ]
    assert required_missing == [], required_missing
    assert any(x.name == "anthropic credentials" and x.found for x in rows)


def test_credential_status_via_ant(monkeypatch):
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_AUTH_TOKEN", raising=False)
    monkeypatch.setattr(proc, "which", lambda n: "/usr/bin/ant" if n == "ant" else None)
    monkeypatch.setattr(
        proc,
        "run",
        lambda argv, **kw: proc.ProcResult(argv, 0, "Active profile: default\n", "", 0.0),
    )
    assert doctor.credential_status().found
    monkeypatch.setattr(
        proc, "run", lambda argv, **kw: proc.ProcResult(argv, 1, "Not logged in\n", "", 0.0)
    )
    assert not doctor.credential_status().found


# --- hunt ---------------------------------------------------------------


class FakeLedger:
    def __init__(self, tus, functions):
        self.tus = {t.id: t for t in tus}
        self.functions = functions
        self.findings: list[Finding] = []
        self.claims = []
        self.runs = []

    def list_functions(
        self, backend, target_key, status=None, order_by_attack_score=True, limit=None
    ):
        from fver.ledger.api import FunctionRow

        return [FunctionRow(f, Status.NOT_ATTEMPTED, None) for f in self.functions]

    def get_tu(self, tu_id):
        return self.tus.get(tu_id)

    def start_run(self, command, backend, target_key, meta):
        self.runs.append(("start", command))
        return "run1"

    def end_run(self, run_id, ok, message=""):
        self.runs.append(("end", ok, message))

    def delete_findings(self, source_paths, tool=None):
        before = len(self.findings)
        self.findings = [
            f
            for f in self.findings
            if not (f.source_path in source_paths and (tool is None or f.tool == tool))
        ]
        return before - len(self.findings)

    def record_finding(self, f):
        self.findings.append(f)

    def record_claim(self, c):
        self.claims.append(c)

    def close(self):
        pass


class FakeCtx:
    def __init__(self, ws, ledger):
        self.ws = ws
        self.ledger = ledger
        self.config = FverConfig()
        self.config.hunters = HuntersConfig()
        self.target = Target()
        self.backend_name = "null"


class FakeHunter:
    name = "cbmc"

    def doctor(self):
        return []

    def run(self, tus, functions, repo_root, workdir, config):
        return [
            Finding(functions[0].id, functions[0].source_path, 12, "out_of_bounds", "cbmc", "oob"),
            Finding(
                functions[0].id, functions[0].source_path, 11, "bound_reached", "cbmc", "unwind"
            ),
            Finding(None, "src/other.c", None, "signed_overflow", "cbmc", "no fn"),
        ]


def test_hunt_records_findings_and_bug_claims(repo, monkeypatch):
    runner.invoke(app, ["init", "--backend", "null"])
    ws = Workspace.open(repo)
    tu = TranslationUnit("tu1", "src/a.c", str(repo), ["clang", "src/a.c"])
    fns = [FunctionInfo("tu1:f", "f", "tu1", "src/a.c", 1, 1, "int f(void)", "bh")]
    ledger = FakeLedger([tu], fns)
    monkeypatch.setattr(hunt, "enabled_hunters", lambda cfg, only=None: [FakeHunter()])
    findings = hunt.run_hunt(FakeCtx(ws, ledger), None, None, None)
    assert len(findings) == 3 and len(ledger.findings) == 3
    assert all(f.run_id == "run1" for f in ledger.findings)
    assert len(ledger.claims) == 1
    c = ledger.claims[0]
    assert c.status is Status.BUG_FOUND and c.function_id == "tu1:f" and c.body_hash == "bh"
    assert (
        c.message == "cbmc: out_of_bounds at line 12"
        and c.extra["finding"]["kind"] == "out_of_bounds"
    )
    assert ledger.runs == [("start", "hunt"), ("end", True, "3 finding(s)")]


def test_hunt_without_scan_tells_user(repo):
    runner.invoke(app, ["init", "--backend", "null"])
    ws = Workspace.open(repo)
    assert hunt.run_hunt(FakeCtx(ws, FakeLedger([], [])), None, None, None) == []


def test_hunt_filters_by_function_and_file(repo):
    runner.invoke(app, ["init", "--backend", "null"])
    ws = Workspace.open(repo)
    tu = TranslationUnit("tu1", "src/a.c", str(repo), ["clang", "src/a.c"])
    fns = [
        FunctionInfo("tu1:f", "f", "tu1", "src/a.c", 1, 1, "int f(void)", "bh"),
        FunctionInfo("tu1:g", "g", "tu1", "src/a.c", 2, 2, "int g(void)", "bh2"),
    ]
    ctx = FakeCtx(ws, FakeLedger([tu], fns))
    tus, functions = hunt._load_index(ctx, "g", None)
    assert [f.name for f in functions] == ["g"] and [t.id for t in tus] == ["tu1"]
    tus, functions = hunt._load_index(ctx, None, "src/none.c")
    assert functions == [] and tus == []
