"""Logging setup: rich console for humans, plain files for the record."""

from __future__ import annotations

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path

from rich.console import Console
from rich.logging import RichHandler

console = Console()
err_console = Console(stderr=True)


LOG_FILE = "fver.log"
_LOG_MAX_BYTES = 5 * 1024 * 1024
_LOG_BACKUPS = 2


class _RunContext(logging.Filter):
    """Stamps every record with the command that produced it."""

    def __init__(self, run_name: str) -> None:
        super().__init__()
        self.run_name = run_name

    def filter(self, record: logging.LogRecord) -> bool:
        record.run = self.run_name
        return True


def setup_logging(
    log_dir: Path | None = None,
    verbose: bool = False,
    run_name: str = "fver",
    console: bool = True,
) -> logging.Logger:
    """Every command logs the same way: warnings and above to the terminal
    (`verbose` lowers that to debug), everything to one rotating file,
    <repo>/.fver/logs/fver.log, tagged with the command name."""
    root = logging.getLogger("fver")
    root.setLevel(logging.DEBUG)
    # Logger filters only see records logged to that logger itself, not to
    # children, so the command tag goes on the handlers.
    tag = _RunContext(run_name)
    for h in root.handlers:
        for f in list(h.filters):
            if isinstance(f, _RunContext):
                h.removeFilter(f)
        h.addFilter(tag)
    if console and not any(isinstance(h, RichHandler) for h in root.handlers):
        h = RichHandler(console=err_console, show_path=False, rich_tracebacks=True, markup=True)
        h.setLevel(logging.DEBUG if verbose else logging.INFO)
        h.addFilter(tag)
        root.addHandler(h)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        path = log_dir / LOG_FILE
        already = any(
            isinstance(h, RotatingFileHandler) and Path(h.baseFilename) == path.resolve()
            for h in root.handlers
        )
        if not already:
            fh = RotatingFileHandler(
                path, maxBytes=_LOG_MAX_BYTES, backupCount=_LOG_BACKUPS, encoding="utf-8"
            )
            fh.setLevel(logging.DEBUG)
            fh.setFormatter(
                logging.Formatter("%(asctime)s %(levelname)s [%(run)s] %(name)s: %(message)s")
            )
            fh.addFilter(tag)
            root.addHandler(fh)
    return root
