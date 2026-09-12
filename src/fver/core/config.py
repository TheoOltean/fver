"""The one configuration file: <repo>/.fver/config.toml.

Created by `fver init`, ignored by git (it holds the API key), edited by
hand. Everything not written in it takes the default below.
"""

from __future__ import annotations

import re
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
from pydantic import BaseModel, Field

CONFIG_DIR_NAME = ".fver"
CONFIG_FILE_NAME = "config.toml"


class ProjectConfig(BaseModel):
    backend: str = "refinedc"  # "null" only in fver's own tests


class SourcesConfig(BaseModel):
    """Which .c files to prove, as repo-relative globs. Exclude wins."""

    include: list[str] = Field(default_factory=lambda: ["**/*.c"])
    exclude: list[str] = Field(
        default_factory=lambda: ["**/test/**", "**/tests/**", "**/third_party/**", "**/vendor/**"]
    )


class ModelConfig(BaseModel):
    # Falls back to ANTHROPIC_API_KEY / ANTHROPIC_AUTH_TOKEN / an
    # `ant auth login` profile when unset.
    api_key: str | None = None
    # Only for keys that are not scoped to a workspace (the API then demands one):
    # console.anthropic.com -> Settings -> Workspaces.
    workspace_id: str | None = None
    base_url: str | None = None
    model: str = "claude-fable-5-1"
    effort: str = "high"  # low | medium | high | xhigh | max
    max_tokens: int = 32000
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


class FverConfig(BaseModel):
    project: ProjectConfig = Field(default_factory=ProjectConfig)
    sources: SourcesConfig = Field(default_factory=SourcesConfig)
    model: ModelConfig = Field(default_factory=ModelConfig)
    budget: BudgetConfig = Field(default_factory=BudgetConfig)


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
# it), so the API key lives here. Only the settings you are expected to touch
# are written; every other key and its default:
#
{defaults}"""

ALWAYS_WRITTEN = (
    "model.api_key",
    "model.workspace_id",
    "model.model",
    "model.effort",
    "budget.max_usd_per_run",
    "budget.max_usd_per_function",
)

SECTION_COMMENTS = {
    "model": (
        "api_key: your Anthropic key (or leave empty and export ANTHROPIC_API_KEY).\n"
        "# workspace_id: only if the API says the key is not scoped to a workspace.\n"
        "# effort: low | medium | high | xhigh | max."
    ),
    "budget": "Money, attempt and size caps, per function and per run.",
    "sources": "Which .c files to prove (repo-relative globs; exclude wins).",
}


def _with_comments(toml: str) -> str:
    defaults = tomli_w.dumps(_drop_none(FverConfig().model_dump(mode="json"))).rstrip()
    defaults = "\n".join(f"#   {line}".rstrip() for line in defaults.splitlines())
    out: list[str] = [CONFIG_HEADER.format(defaults=defaults).rstrip("\n")]
    for line in toml.splitlines():
        m = re.match(r"^\[([a-z_]+)", line)
        if m and m.group(1) in SECTION_COMMENTS:
            if out and out[-1].strip():
                out.append("")
            out.append(f"# {SECTION_COMMENTS[m.group(1)]}")
        out.append(line)
    return "\n".join(out).rstrip() + "\n"


def dumps_config(cfg: FverConfig) -> str:
    """The file `fver init` writes: the few knobs a user touches, always
    present even at their defaults, plus anything else that differs."""
    full = cfg.model_dump(mode="json")
    data = _diff(FverConfig().model_dump(mode="json"), full)
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
