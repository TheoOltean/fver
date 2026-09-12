"""Configuration schema and loading.

Layout:
  <repo>/.fver/config.toml           project config (created by `fver init`)
  ~/.fver/config.toml                user-level config (credentials, model), merged
                                     underneath; FVER_HOME overrides the ~/.fver location

Backend-specific settings live under [backend.<name>] and are passed to the
backend untouched, so adding a backend never requires editing this file.
"""

from __future__ import annotations

import os
import re
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, Field

from fver.core.models import Target

CONFIG_DIR_NAME = ".fver"
CONFIG_FILE_NAME = "config.toml"


class ProjectConfig(BaseModel):
    name: str = "project"
    backend: str = "refinedc"
    property_class: str = "ub_free"


class BuildConfig(BaseModel):
    # Path to an existing compile_commands.json, relative to repo root.
    compile_commands: str | None = None
    # Or a shell command that produces one (run from repo root), e.g. "bear -- make".
    capture_command: str | None = None
    # Glob patterns (relative to repo root) selecting which sources to verify.
    include: list[str] = Field(default_factory=lambda: ["**/*.c"])
    exclude: list[str] = Field(
        default_factory=lambda: ["**/test/**", "**/tests/**", "**/third_party/**", "**/vendor/**"]
    )
    # Used when no compile_commands.json is available: every included .c file
    # is compiled with these flags.
    fallback_flags: list[str] = Field(default_factory=lambda: ["-std=c11"])


class TargetConfig(BaseModel):
    triple: str = "x86_64-linux-gnu"
    compiler: str = "cc"
    int_bits: int = 32
    long_bits: int = 64
    pointer_bits: int = 64
    char_signed: bool = True
    little_endian: bool = True

    def to_target(self) -> Target:
        return Target(**self.model_dump())


class ModelConfig(BaseModel):
    # Credentials. Prefer the user-level config (~/.fver/config.toml,
    # `fver config set --user model.api_key sk-ant-...`) so the key never lands
    # in a committed project config. Falls back to ANTHROPIC_API_KEY /
    # ANTHROPIC_AUTH_TOKEN / an `ant auth login` profile when unset.
    api_key: str | None = None
    base_url: str | None = None
    model: str = "claude-fable-5-1"
    effort: str = "high"  # low | medium | high | xhigh | max
    max_tokens: int = 32000
    # Server-side refusal fallbacks (Claude API only). Disable on other platforms.
    fallbacks: bool = True
    # Cache the stable prompt prefix (backend docs, examples).
    prompt_caching: bool = True
    timeout_seconds: float = 1800.0


class BudgetConfig(BaseModel):
    max_attempts_per_function: int = 8
    max_usd_per_function: float = 10.0
    max_usd_per_run: float = 200.0
    checker_timeout_seconds: int = 600
    # How many functions `fver verify` works on concurrently.
    parallelism: int = 2


class VerifyConfig(BaseModel):
    """Defaults for `fver verify`, `fver next` and `fver task`. Command-line
    flags override these for one run."""

    # Stop a run after this many functions; 0 means no limit.
    limit: int = 0
    # Also retry functions an earlier run left UNRESOLVED.
    retry_unresolved: bool = False
    # Place each function's unverified internal callees before it.
    follow_callees: bool = True
    # `fver next`: how many functions to list.
    next_limit: int = 10
    # `fver task`: include the annotation-language reference in plain output.
    task_reference: bool = True


class HuntersConfig(BaseModel):
    """`fver hunt` always runs CBMC. The sanitizer hunter rebuilds and runs
    the project's own tests, so it needs to know how: set test_command."""

    cbmc_unwind: int = 8
    cbmc_timeout_seconds: int = 300
    # Command that runs the project's tests, e.g. "make test". Unset: the
    # sanitizer hunter has nothing to run.
    test_command: str | None = None


class FverConfig(BaseModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)
    target: TargetConfig = Field(default_factory=TargetConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    verify: VerifyConfig = Field(default_factory=VerifyConfig)
    hunters: HuntersConfig = Field(default_factory=HuntersConfig)
    # Free-form per-backend settings: config.backend["refinedc"]["refinedc_bin"]
    backend: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def backend_settings(self, name: str | None = None) -> dict[str, Any]:
        return dict(self.backend.get(name or self.project.backend, {}))


def user_home() -> Path:
    """fver's own directory for the user: ~/.fver (or $FVER_HOME)."""
    return Path(os.environ.get("FVER_HOME") or (Path.home() / CONFIG_DIR_NAME))


def user_config_path() -> Path:
    return user_home() / CONFIG_FILE_NAME


def _deep_merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = dict(base)
    for k, v in over.items():
        if isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = _deep_merge(out[k], v)
        else:
            out[k] = v
    return out


def load_project_config(repo_root: Path) -> FverConfig:
    """Only the project's own file, without the user-level layer. This is what
    `fver config set` edits, so user settings (tool paths, credentials) never
    get copied into a file that is meant to be committed."""
    pp = repo_root / CONFIG_DIR_NAME / CONFIG_FILE_NAME
    data = tomllib.loads(pp.read_text(encoding="utf-8", errors="replace")) if pp.exists() else {}
    return FverConfig.model_validate(data)


def load_config(repo_root: Path) -> FverConfig:
    """Load user defaults then the project config on top."""
    data: dict[str, Any] = {}
    up = user_config_path()
    if up.exists():
        data = _deep_merge(data, tomllib.loads(up.read_text(encoding="utf-8", errors="replace")))
    pp = repo_root / CONFIG_DIR_NAME / CONFIG_FILE_NAME
    if pp.exists():
        data = _deep_merge(data, tomllib.loads(pp.read_text(encoding="utf-8", errors="replace")))
    return FverConfig.model_validate(data)


def _drop_none(obj: Any) -> Any:
    """TOML has no null; omit None-valued keys (they mean 'unset')."""
    if isinstance(obj, dict):
        return {k: _drop_none(v) for k, v in obj.items() if v is not None}
    if isinstance(obj, list):
        return [_drop_none(v) for v in obj]
    return obj


def _diff(defaults: Any, current: Any) -> Any:
    """Keep only keys whose value differs from the default."""
    if isinstance(current, dict) and isinstance(defaults, dict):
        out: dict[str, Any] = {}
        for k, v in current.items():
            if k not in defaults:
                out[k] = v
                continue
            sub = _diff(defaults[k], v)
            if isinstance(sub, dict):
                if sub:
                    out[k] = sub
            elif v != defaults[k]:
                out[k] = v
        return out
    return current


CONFIG_HEADER = """\
# fver project configuration. Only settings that differ from the defaults are
# written here; `fver config show` prints every setting with its effective
# value and `fver config set <key> <value>` changes one. .fver/GUIDE.md
# documents all of them.
"""

SECTION_COMMENTS = {
    "project": "This project. The proof stack is RefinedC on Rocq; there is nothing to choose.",
    "target": (
        "The machine the proofs are for, detected from your C compiler. Proofs depend on\n"
        "# integer widths, pointer size and char signedness, so they are recorded with every\n"
        "# result. Change these only to verify for a different platform."
    ),
    "build": (
        "How fver learns each file's compiler flags. Normally nothing is needed: at scan\n"
        "# time it uses compile_commands.json if present, `bear -- make` for Makefile\n"
        "# projects, and fallback_flags otherwise. include/exclude choose which sources count."
    ),
    "model": (
        "Which Claude model proves, and how hard it thinks. The API key belongs in\n"
        "# ~/.fver/config.toml (`fver config set --user model.api_key ...`), never here."
    ),
    "budget": "Money and attempt caps, per function and per run.",
    "verify": "Defaults for `fver verify`, `fver next` and `fver task`; flags override for one run.",
    "hunters": "Bug finders. CBMC always runs; set test_command to also run your tests under sanitizers.",
    "backend": "Proof-checker settings: tool paths (`fver setup` records them in ~/.fver), extra includes.",
}


def _with_comments(toml: str) -> str:
    out: list[str] = [CONFIG_HEADER.rstrip("\n")]
    for line in toml.splitlines():
        m = re.match(r"^\[([a-z_]+)", line)
        if m and m.group(1) in SECTION_COMMENTS:
            if out and out[-1].strip():
                out.append("")
            out.append(f"# {SECTION_COMMENTS[m.group(1)]}")
        out.append(line)
    return "\n".join(out).rstrip() + "\n"


def dumps_config(cfg: FverConfig, minimal: bool = True) -> str:
    """Serialise a config. `minimal` writes only values that differ from the
    defaults, so a project file never shadows user-level settings such as the
    model or API key with copies of the defaults, and adds explanatory
    comments. `fver config show` prints the effective, fully merged
    configuration without comments."""
    data = cfg.model_dump(mode="json")
    if not minimal:
        return tomli_w.dumps(_drop_none(data))
    data = _diff(FverConfig().model_dump(mode="json"), data)
    # Always keep the project name so the file is self-describing. The backend
    # is only written when it is not the stack (the null backend, for tests).
    data.setdefault("project", {})
    data["project"].setdefault("name", cfg.project.name)
    return _with_comments(tomli_w.dumps(_drop_none(data)))


def save_config(repo_root: Path, cfg: FverConfig) -> Path:
    path = repo_root / CONFIG_DIR_NAME / CONFIG_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps_config(cfg), encoding="utf-8")
    return path


def default_config_toml(project_name: str, backend: str = "refinedc") -> str:
    cfg = FverConfig(project=ProjectConfig(name=project_name, backend=backend))
    return dumps_config(cfg)
