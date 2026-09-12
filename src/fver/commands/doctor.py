"""fver doctor: check external tools and API credentials."""

from __future__ import annotations

import os
import platform
import sys

import typer
from rich.table import Table

from fver.core.models import ToolStatus
from fver.util import proc
from fver.util.log import console


def _tool(
    name: str, required: bool, hint: str, version_argv: list[str] | None = None
) -> ToolStatus:
    path = proc.which(name)
    version = proc.version_of(version_argv or [name, "--version"]) if path else None
    return ToolStatus(
        name=name, found=path is not None, path=path, version=version, required=required, hint=hint
    )


def credential_status(config_key: str | None = None) -> ToolStatus:
    if config_key:
        return ToolStatus("anthropic credentials", True, version="config model.api_key", hint="")
    if os.environ.get("ANTHROPIC_API_KEY"):
        return ToolStatus("anthropic credentials", True, version="ANTHROPIC_API_KEY", hint="")
    if os.environ.get("ANTHROPIC_AUTH_TOKEN"):
        return ToolStatus("anthropic credentials", True, version="ANTHROPIC_AUTH_TOKEN", hint="")
    if proc.which("ant"):
        r = proc.run(["ant", "auth", "status"], timeout=20)
        text = (r.stdout + r.stderr).lower()
        if (
            r.ok
            and ("active" in text or "logged in" in text or "profile" in text)
            and "not logged" not in text
        ):
            return ToolStatus("anthropic credentials", True, version="ant auth profile", hint="")
    return ToolStatus(
        "anthropic credentials",
        False,
        required=False,  # informational: session mode needs none
        hint="API mode only: `fver config set --user model.api_key ...`, "
        "ANTHROPIC_API_KEY, or `ant auth login`. Session mode (Claude Code) needs none.",
    )


def collect_statuses(online: bool = False) -> tuple[list[ToolStatus], str | None]:
    """Returns (rows, error). Tolerates a missing workspace: then only generic tools are checked."""
    rows: list[ToolStatus] = []
    rows.append(
        ToolStatus("python", True, sys.executable, platform.python_version(), required=True)
    )
    try:
        import anthropic

        rows.append(ToolStatus("anthropic sdk", True, version=anthropic.__version__, required=True))
    except Exception:  # pragma: no cover  # noqa: BLE001
        rows.append(ToolStatus("anthropic sdk", False, required=True, hint="pip install anthropic"))

    compiler = "cc"
    model_id = "claude-fable-5-1"
    backend_error: str | None = None
    ctx = None
    try:
        from fver.core.context import AppContext

        try:
            ctx = AppContext.load(need_backend=True)
        except Exception as e:  # backend construction failed; retry without it  # noqa: BLE001
            backend_error = str(e)
            try:
                ctx = AppContext.load(need_backend=False)
            except Exception:  # noqa: BLE001
                ctx = None
    except Exception as e:  # pragma: no cover  # noqa: BLE001
        backend_error = str(e)
    if ctx is not None:
        compiler = ctx.config.target.compiler
        model_id = ctx.config.model.model

    from fver.util.platform import install_hint

    rows.append(
        _tool(
            compiler,
            True,
            install_hint(
                {"brew": "llvm (or run xcode-select --install)", "*": "clang"},
                note="any C compiler works; set target.compiler",
            ),
        )
    )
    rows.append(_tool("bear", True, "run `fver setup`"))
    try:
        from fver.hunters.base import builtin_hunters

        for cls in builtin_hunters().values():
            rows.extend(cls().doctor())
    except Exception as e:  # pragma: no cover  # noqa: BLE001
        rows.append(ToolStatus("hunters", False, required=False, hint=str(e)))
    if ctx is not None and ctx.backend is not None:
        try:
            rows.extend(ctx.backend.doctor())
        except Exception as e:  # noqa: BLE001
            rows.append(
                ToolStatus(f"backend:{ctx.backend_name}", False, required=True, hint=str(e))
            )
    elif backend_error:
        rows.append(ToolStatus("backend", False, required=True, hint=backend_error))
    rows.append(credential_status(_config_api_key()))

    if online:
        try:
            import anthropic

            client = anthropic.Anthropic()
            m = client.models.retrieve(model_id)
            rows.append(
                ToolStatus("api (online)", True, version=getattr(m, "id", model_id), required=True)
            )
        except Exception as e:  # noqa: BLE001
            rows.append(
                ToolStatus("api (online)", False, required=True, hint=f"{type(e).__name__}: {e}")
            )
    if ctx is not None:
        ctx.close()
    return rows, backend_error


def render(rows: list[ToolStatus]) -> int:
    table = Table(title="fver doctor", show_lines=False)
    table.add_column("tool")
    table.add_column("status")
    table.add_column("version / path")
    table.add_column("hint")
    missing_required = 0
    for r in rows:
        if r.found:
            status = "[green]ok[/]"
        elif r.required:
            status = "[red]missing[/]"
            missing_required += 1
        else:
            status = "[yellow]not set[/]"
        table.add_row(r.name, status, r.version or r.path or "", "" if r.found else r.hint)
    console.print(table)
    return 1 if missing_required else 0


def register(app: typer.Typer) -> None:
    @app.command("doctor")
    def doctor(
        online: bool = typer.Option(
            False, "--online", help="Also make one small API call to verify credentials."
        ),
    ) -> None:
        """Check that every tool fver needs is installed (see `fver setup`) and whether API credentials are set."""
        rows, _ = collect_statuses(online=online)
        code = render(rows)
        raise typer.Exit(code=code)


def _config_api_key() -> str | None:
    try:
        from fver.core.workspace import Workspace

        return Workspace.open().config.model.api_key
    except Exception:  # noqa: BLE001 - outside a project: no config key
        return None
