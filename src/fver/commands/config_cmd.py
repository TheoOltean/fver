"""`fver config`: print the effective configuration; `fver config set` changes one value."""

from __future__ import annotations

import tomllib
from typing import Any

import tomli_w
import typer
from pydantic import ValidationError

from fver.core.config import FverConfig, _drop_none, load_config, save_config
from fver.core.workspace import Workspace

config_app = typer.Typer(
    help="Print the effective configuration, or `set` one value in .fver/config.toml.",
    invoke_without_command=True,
)


def parse_value(raw: str) -> Any:
    """Parse as a TOML literal (true, 3, 1.5, "x", ["a","b"]); fall back to the bare string."""
    try:
        return tomllib.loads(f"v = {raw}")["v"]
    except tomllib.TOMLDecodeError:
        return raw


def get_value(cfg: FverConfig, key: str) -> Any:
    node: Any = cfg.model_dump(mode="json")
    for part in key.split("."):
        if not isinstance(node, dict) or part not in node:
            raise KeyError(key)
        node = node[part]
    return node


def set_value(cfg: FverConfig, key: str, raw: str) -> FverConfig:
    data = cfg.model_dump(mode="json")
    parts = key.split(".")
    node = data
    for part in parts[:-1]:
        if part not in node or not isinstance(node[part], dict):
            node[part] = {}
        node = node[part]
    node[parts[-1]] = parse_value(raw)
    try:
        return FverConfig.model_validate(data)
    except ValidationError as e:
        raise typer.BadParameter(f"invalid value for {key}: {e.errors()[0]['msg']}") from e


def _render(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, dict):
        return tomli_w.dumps(_drop_none(value)).rstrip()
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, list):
        return tomli_w.dumps({"v": value}).split("=", 1)[1].strip()
    return str(value)


@config_app.callback()
def config_show(ctx: typer.Context) -> None:
    """Print every setting with its effective value (the file, then the defaults)."""
    if ctx.invoked_subcommand is not None:
        return
    ws = Workspace.open()
    typer.echo(f"# {ws.config_path}")
    typer.echo(tomli_w.dumps(_drop_none(ws.config.model_dump(mode="json"))))


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Dotted key, e.g. model.api_key or budget.max_usd_per_run"),
    value: str = typer.Argument(
        ..., help="TOML literal or bare string, e.g. 5.0, true, sk-ant-..."
    ),
) -> None:
    """Change one value in .fver/config.toml."""
    ws = Workspace.open()
    new_cfg = set_value(load_config(ws.repo_root), key, value)
    save_config(ws.repo_root, new_cfg)
    typer.echo(f"{key} = {_render(get_value(new_cfg, key))}")


def register(app: typer.Typer) -> None:
    app.add_typer(config_app, name="config")
