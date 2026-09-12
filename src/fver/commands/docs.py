"""`fver docs`: the documentation that `fver init` writes to `.fver/GUIDE.md`.

The file is meant to be read by whoever works in the repository next, human
or model: the `.fver/` layout, every command with its options (generated
from the CLI itself so it cannot drift), every configuration key with its
default, and the session-mode proving workflow. `fver docs --write`
refreshes it after an upgrade.
"""

from __future__ import annotations

from collections.abc import Iterator
from typing import Any

import typer

from fver.core.config import FverConfig, dumps_config

LAYOUT = """\
# .fver/

State written by `fver`. Your source files are never modified; everything
fver produces lives in this directory.

| Path | What | Commit it? |
|---|---|---|
| `config.toml` | project configuration | yes |
| `GUIDE.md` | this file: layout, commands, configuration, proving workflow | yes |
| `ledger.sqlite` | what is proven, what is not, and why | yes (recommended) |
| `proofs/` | accepted annotations and proofs, mirroring the source tree | yes |
| `external/` | trusted specs for libc and other external functions | yes |
| `work/` | build capture, preprocessed files, function index | no (gitignored) |
| `backend/` | the proof backend's private project | no |
| `cache/` | content-addressed results | no |
| `logs/` | run logs and LLM transcripts | no |
| `scratch/` | a place for submissions while proving by hand | no |

Workflow: `fver doctor` -> `fver scan` -> `fver hunt` -> `fver verify` -> `fver status`.
Run every command from anywhere inside the repository.
"""

PROVER_WORKFLOW = """\
# Proving C functions with fver

You are the prover. fver supplies the task, the rules, the proof checker,
the audit and the bookkeeping; you supply the annotations and proofs. The
checker is the only judge: nothing is verified until `fver check` says so.

## Goal

Get functions to `verified` status, highest attack surface first, without
changing any C code and without escape hatches.

## How it fits together

- `fver next` lists what to prove, callees before callers, so contracts
  exist before you need them.
- `fver task <fn>` prints the packet for one function: the annotation
  language reference (read it once per session, then use `--no-reference`),
  the function, its file context, the contracts of verified callees, trusted
  external specs, and the previous accepted submission if the code changed.
- Write the annotated function (the full definition, code unchanged) to a
  scratch file under `.fver/` such as `.fver/scratch/<fn>.c`. Never edit the
  user's source files. Helper lemmas, if needed, go in a second file.
- `fver check <fn> --submission .fver/scratch/<fn>.c [--lemmas ...]` runs
  the guardrails, the checker and the audit, records the result, and quotes
  the goal the checker could not close. Exit 0 means verified.
- Iterate on the feedback. Stop when verified, when the budget you were
  given is spent, or when the checker reports a real bug.
- If the code has a genuine defect that no honest precondition rules out,
  do not force a proof: submit a reply that starts with `BUG:` and explains
  the triggering input. fver records it as a suspected bug for a human.
- After you edit any C code, run `fver changed` to see which proofs went
  stale and re-prove them; a stale proof is a proof of old code.

## Rules the checker enforces

- Preconditions must be the weakest that make the function safe. A proof
  that demands `false` of the caller is rejected.
- No admitted goals, trusted functions, skipped functions or new axioms.
- The submission is the whole function every time.

## Reporting

Tell the user what is verified, what is stale, and any `BUG:` reports, in
plain words. `fver status` and `fver show <fn>` have the details.
"""


# Typer may ship its own copy of click, so the command objects are inspected
# by shape (list_commands / params / opts) rather than by isinstance.


def _root_group() -> Any:
    from fver.cli import app  # lazy: cli.py imports the command modules

    return typer.main.get_command(app)


def _is_group(cmd: Any) -> bool:
    return hasattr(cmd, "list_commands") and hasattr(cmd, "get_command")


def _walk(group: Any, ctx: Any, prefix: str = "") -> Iterator[tuple[str, Any, bool]]:
    for name in group.list_commands(ctx):
        cmd = group.get_command(ctx, name)
        if cmd is None or getattr(cmd, "hidden", False):
            continue
        full = f"{prefix}{name}"
        if _is_group(cmd):
            yield full, cmd, True
            sub_ctx = cmd.make_context(name, [], parent=ctx, resilient_parsing=True)
            yield from _walk(cmd, sub_ctx, prefix=full + " ")
        else:
            yield full, cmd, False


def _cell(text: str) -> str:
    return " ".join(text.split()).replace("|", "\\|")


def command_reference() -> str:
    """One section per command, options in a table, generated from the CLI."""
    root = _root_group()
    ctx = root.make_context("fver", [], resilient_parsing=True)
    out = [
        "## Commands",
        "",
        "Flags override the configuration for one run; without them the values in",
        "`config.toml` apply. `fver <command> --help` prints the same information.",
        "",
    ]
    for full, cmd, is_group in _walk(root, ctx):
        out.append(f"### `fver {full}`")
        out.append("")
        if cmd.help:
            out.append(cmd.help.strip())
            out.append("")
        if is_group:
            continue
        rows: list[tuple[str, str]] = []
        for p in cmd.params:
            if getattr(p, "hidden", False):
                continue
            opts = list(getattr(p, "opts", []))
            if p.param_type_name == "argument":
                how = "argument" + ("" if p.required else ", optional")
                rows.append((f"`{p.human_readable_name}`", how))
            elif opts:
                names = ", ".join(f"`{o}`" for o in [*opts, *getattr(p, "secondary_opts", [])])
                rows.append((names, _cell(getattr(p, "help", None) or "")))
        if rows:
            out.append("| Option | Meaning |")
            out.append("|---|---|")
            out.extend(f"| {a} | {b} |" for a, b in rows)
            out.append("")
    return "\n".join(out)


def config_reference() -> str:
    """Every configuration key with its default value."""
    toml = dumps_config(FverConfig(), minimal=False).rstrip()
    return (
        "## Configuration\n"
        "\n"
        "Settings live in `.fver/config.toml` (commit it). Optional user-level defaults\n"
        "in `~/.config/fver/config.toml` are merged underneath; put credentials such as\n"
        "`model.api_key` there, never in the project file. Read and write with\n"
        "`fver config get <key>`, `fver config set <key> <value>` (`--user` for the\n"
        "user-level file) and `fver config show` (the effective merged configuration).\n"
        "\n"
        "Sections: `[project]` backend and property class; `[build]` how to find or\n"
        "capture the build; `[target]` the ABI the proofs are for; `[model]` which LLM\n"
        "and how; `[budget]` money and attempt limits; `[verify]` defaults for `fver\n"
        "verify`, `fver next` and `fver task`; `[hunters]` which bug finders run;\n"
        "`[backend.<name>]` settings passed to the proof backend.\n"
        "\n"
        "Every key with its default:\n"
        "\n"
        "```toml\n"
        f"{toml}\n"
        "```\n"
    )


def render_docs() -> str:
    return "\n".join([LAYOUT, command_reference(), config_reference(), PROVER_WORKFLOW])


def register(app: typer.Typer) -> None:
    @app.command("docs")
    def docs(
        write: bool = typer.Option(
            False, "--write", help="Refresh .fver/GUIDE.md instead of printing."
        ),
    ) -> None:
        """Print the command, configuration and workflow reference (what `fver init` writes to .fver/GUIDE.md)."""
        text = render_docs()
        if not write:
            typer.echo(text)
            return
        from fver.core.workspace import Workspace

        ws = Workspace.open()
        path = ws.root / "GUIDE.md"
        path.write_text(text, encoding="utf-8")
        typer.echo(f"wrote {path}")
