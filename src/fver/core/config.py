"""Configuration schema and loading.

One file per project: <repo>/.fver/config.toml, created by `fver init` and
ignored by git (it holds the API key). Everything not written there takes
the default below.

Backend-specific settings live under [backend.<name>] and are passed to the
backend untouched, so adding a backend never requires editing this file.
"""

from __future__ import annotations

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
    # Defaults to the repository directory's name.
    name: str | None = None
    backend: str = "refinedc"


class BuildConfig(BaseModel):
    # Path to an existing compile_commands.json, relative to repo root.
    compile_commands: str | None = None
    # Or a shell command that produces one (run from repo root), e.g. "bear -- make -B" (needs bear).
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
    """The platform the proofs are for. Detected from the compiler at init and
    scan; every key here is an override for cross-platform work."""

    compiler: str = "cc"
    triple: str | None = None
    int_bits: int | None = None
    long_bits: int | None = None
    pointer_bits: int | None = None
    char_signed: bool | None = None
    little_endian: bool | None = None

    def apply(self, detected: Target) -> Target:
        over = {k: v for k, v in self.model_dump().items() if v is not None}
        return Target(**{**detected.__dict__, **over})

    def to_target(self) -> Target:
        """The overrides on top of the default target (no detection)."""
        return self.apply(Target())


class ModelConfig(BaseModel):
    # Falls back to ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / an
    # `ant auth login` profile when unset.
    api_key: str | None = None
    base_url: str | None = None
    model: str = "claude-fable-5-1"
    effort: str = "high"  # low | medium | high | xhigh | max
    max_tokens: int = 32000
    # Cache the stable prompt prefix (backend docs, examples).
    prompt_caching: bool = True
    timeout_seconds: float = 1800.0


class BudgetConfig(BaseModel):
    max_attempts_per_function: int = 8
    max_usd_per_function: float = 10.0
    max_usd_per_run: float = 200.0
    checker_timeout_seconds: int = 600
    # How many functions one `fver prove` may take on; 0 means no limit.
    max_functions_per_run: int = 0
    # How many functions are proven concurrently.
    parallelism: int = 2


class HuntersConfig(BaseModel):
    """CBMC, which `fver prove` runs over each function before proving it."""

    cbmc_unwind: int = 8
    cbmc_timeout_seconds: int = 300


class FverConfig(BaseModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    build: BuildConfig = Field(default_factory=BuildConfig)
    target: TargetConfig = Field(default_factory=TargetConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)
    hunters: HuntersConfig = Field(default_factory=HuntersConfig)
    # Free-form per-backend settings: config.backend["refinedc"]["refinedc_bin"]
    backend: dict[str, dict[str, Any]] = Field(default_factory=dict)

    def backend_settings(self, name: str | None = None) -> dict[str, Any]:
        return dict(self.backend.get(name or self.project.backend, {}))


def load_config(repo_root: Path) -> FverConfig:
    """The project's config.toml over the defaults. An empty string means
    'not filled in' and counts as unset."""
    pp = repo_root / CONFIG_DIR_NAME / CONFIG_FILE_NAME
    data = tomllib.loads(pp.read_text(encoding="utf-8", errors="replace")) if pp.exists() else {}
    return FverConfig.model_validate(_drop_empty(data))


def _drop_empty(obj: Any) -> Any:
    if isinstance(obj, dict):
        return {k: _drop_empty(v) for k, v in obj.items() if v != ""}
    return obj


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
# fver configuration for this project. Not committed (.fver/.gitignore lists
# it), so the API key lives here. Only settings that differ from the defaults
# are written; `fver config` prints every setting with its effective value and
# `fver config set <key> <value>` changes one. .fver/GUIDE.md documents them.
"""

ALWAYS_WRITTEN = (
    "model.api_key",
    "model.model",
    "model.effort",
    "budget.max_usd_per_run",
    "budget.max_usd_per_function",
)

SECTION_COMMENTS = {
    "project": "Project name (defaults to the directory name).",
    "target": (
        "Overrides for the platform the proofs are for (normally detected from the compiler\n"
        "# and not written here). Set only to verify for a different platform."
    ),
    "build": (
        "Normally nothing: fver reads the source directly with every header directory in\n"
        "# the repository on the include path. If files fail to read because the build\n"
        "# generates headers or defines macros, export the build's compile_commands.json\n"
        "# and set compile_commands, or set capture_command. include/exclude pick the sources."
    ),
    "model": (
        "api_key: your Anthropic key (or leave empty and export ANTHROPIC_API_KEY).\n"
        "# effort: low | medium | high | xhigh | max."
    ),
    "budget": "Money, attempt and size caps, per function and per run.",
    "hunters": "CBMC, run over each function before it is sent to the prover.",
    "backend": (
        "Proof-checker settings. Tools are found in the opam switch `fver setup` creates;\n"
        "# refinedc_bin / coqc_bin / dune_bin override that."
    ),
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
    defaults, with explanatory comments; `fver config` prints the effective
    configuration in full without them."""
    data = cfg.model_dump(mode="json")
    if not minimal:
        return tomli_w.dumps(_drop_none(data))
    full = data
    data = _diff(FverConfig().model_dump(mode="json"), data)
    # The few knobs a user is expected to touch are always present, so the
    # file shows them even at their defaults. Everything else appears only
    # when changed (`fver config set`), including the backend (null = tests).
    for dotted in ALWAYS_WRITTEN:
        sec, key = dotted.split(".")
        value = full[sec][key]
        data.setdefault(sec, {})[key] = "" if value is None else value  # "" = fill me in
    return _with_comments(tomli_w.dumps(_drop_none(data)))


def save_config(repo_root: Path, cfg: FverConfig) -> Path:
    path = repo_root / CONFIG_DIR_NAME / CONFIG_FILE_NAME
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(dumps_config(cfg), encoding="utf-8")
    return path


def default_config_toml(project_name: str | None = None, backend: str = "refinedc") -> str:
    cfg = FverConfig(project=ProjectConfig(name=project_name, backend=backend))
    return dumps_config(cfg)
