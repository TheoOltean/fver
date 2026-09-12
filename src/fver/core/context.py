"""AppContext: what every command needs, opened once.

Commands call `AppContext.load()` and get the workspace, config, ledger,
target and (optionally) the configured backend. Keeping this in one place
means commands never construct backends or ledgers themselves.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path

from fver.backends.base import Backend
from fver.core.config import FverConfig
from fver.core.models import Target
from fver.core.workspace import Workspace
from fver.ledger.api import Ledger, open_ledger


def resolve_target(ws: Workspace, redetect: bool = False) -> Target:
    """The platform the proofs are for, detected from the C compiler and
    cached in .fver/work/target.json."""
    cached = None if redetect else ws.read_state("target")
    if cached:
        return Target(**cached)
    from fver.index.targets import detect_target

    detected = detect_target("cc") or Target(compiler="cc")
    ws.write_state("target", detected.__dict__)
    return detected


@dataclass
class AppContext:
    ws: Workspace
    config: FverConfig
    ledger: Ledger
    target: Target
    backend: Backend | None

    @classmethod
    def load(
        cls, start: Path | None = None, need_backend: bool = True, backend_name: str | None = None
    ) -> AppContext:
        ws = Workspace.open(start)
        cfg = ws.config
        target = resolve_target(ws)
        ledger = open_ledger(ws.ledger_path)
        backend: Backend | None = None
        if need_backend:
            from fver.backends.registry import make_backend

            name = backend_name or cfg.project.backend
            backend = make_backend(name, ws.backend_dir(name), {}, target)
        return cls(ws=ws, config=cfg, ledger=ledger, target=target, backend=backend)

    @property
    def backend_name(self) -> str:
        return self.backend.name if self.backend else self.config.project.backend

    def close(self) -> None:
        self.ledger.close()
