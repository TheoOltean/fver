"""fver config: get / set / show configuration values."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any

import tomli_w
import typer
from pydantic import ValidationError

from fver.core.config import FverConfig, _diff, _drop_none, save_config, user_config_path
from fver.core.workspace import Workspace
from fver.util.log import console

config_app = typer.Typer(
    help="Get or set configuration values in .fver/config.toml.", no_args_is_help=True
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


def set_user_value(key: str, raw: str) -> Path:
    """Set one key in the user-level config, keeping the file minimal."""
    path = user_config_path()
    data = (
        tomllib.loads(path.read_text(encoding="utf-8", errors="replace")) if path.exists() else {}
    )
    new_cfg = set_value(FverConfig.model_validate(data), key, raw)
    path.parent.mkdir(parents=True, exist_ok=True)
    defaults = FverConfig().model_dump(mode="json")
    minimal = _diff(defaults, new_cfg.model_dump(mode="json"))
    path.write_text(tomli_w.dumps(_drop_none(minimal)), encoding="utf-8")
    try:
        os.chmod(path, 0o600)
    except OSError:
        pass
    return path


@config_app.command("get")
def config_get(key: str = typer.Argument(..., help="Dotted key, e.g. model.effort")) -> None:
    ws = Workspace.open()
    try:
        typer.echo(_render(get_value(ws.config, key)))
    except KeyError:
        console.print(f"[red]no such key:[/] {key}")
        raise typer.Exit(code=1) from None


@config_app.command("set")
def config_set(
    key: str = typer.Argument(..., help="Dotted key, e.g. budget.max_usd_per_function"),
    value: str = typer.Argument(
        ..., help='TOML literal or bare string, e.g. 5.0, true, "x", ["a","b"]'
    ),
    user: bool = typer.Option(
        False,
        "--user",
        "-u",
        help="Write to the user-level config (~/.config/fver/config.toml) instead of the "
        "project's. Use this for credentials such as model.api_key.",
    ),
) -> None:
    if user:
        typer.echo(f"{key} set in {set_user_value(key, value)}")
        return
    ws = Workspace.open()
    if key == "model.api_key":
        console.print(
            "[yellow]warning:[/] writing an API key into the project config, which is meant "
            "to be committed. Prefer `fver config set --user model.api_key ...`."
        )
    new_cfg = set_value(ws.config, key, value)
    save_config(ws.repo_root, new_cfg)
    typer.echo(f"{key} = {_render(get_value(new_cfg, key))}")


@config_app.command("path")
def config_path() -> None:
    typer.echo(str(Workspace.open().config_path))


@config_app.command("show")
def config_show() -> None:
    ws = Workspace.open()
    typer.echo(tomli_w.dumps(_drop_none(ws.config.model_dump(mode="json"))))


def register(app: typer.Typer) -> None:
    app.add_typer(config_app, name="config")
