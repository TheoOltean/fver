"""Logging setup: rich console for humans, plain files for the record."""

from __future__ import annotations

import logging
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

console = Console()
err_console = Console(stderr=True)


def setup_logging(
    log_dir: Path | None = None, verbose: bool = False, run_name: str = "fver"
) -> logging.Logger:
    level = logging.DEBUG if verbose else logging.INFO
    root = logging.getLogger("fver")
    root.setLevel(logging.DEBUG)
    if not any(isinstance(h, RichHandler) for h in root.handlers):
        h = RichHandler(console=err_console, show_path=False, rich_tracebacks=True, markup=True)
        h.setLevel(level)
        root.addHandler(h)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        fh = logging.FileHandler(log_dir / f"{run_name}.log")
        fh.setLevel(logging.DEBUG)
        fh.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
        root.addHandler(fh)
    return root
