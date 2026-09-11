"""Backend discovery via entry points, with built-ins as a fallback."""

from __future__ import annotations

import logging
from importlib import import_module
from importlib.metadata import entry_points
from pathlib import Path
from typing import Any

from fver.backends.base import Backend, BackendError
from fver.core.models import Target

_BUILTIN = {
    "refinedc": "fver.backends.refinedc.backend:RefinedCBackend",
    "null": "fver.backends.null:NullBackend",
}


def available_backends() -> dict[str, str]:
    found = dict(_BUILTIN)
    try:
        for ep in entry_points(group="fver.backends"):
            found[ep.name] = ep.value
    except Exception as e:  # noqa: BLE001 - metadata oddities on some installs
        logging.getLogger(__name__).debug("entry point scan failed: %s", e)
    return found


def _load(spec: str) -> type:
    mod, _, attr = spec.partition(":")
    return getattr(import_module(mod), attr)


def make_backend(
    name: str, workspace_dir: Path, settings: dict[str, Any], target: Target
) -> Backend:
    specs = available_backends()
    if name not in specs:
        raise BackendError(f"Unknown backend '{name}'. Available: {', '.join(sorted(specs))}")
    cls = _load(specs[name])
    backend = cls(workspace_dir=workspace_dir, settings=settings, target=target)
    if not isinstance(backend, Backend):
        raise BackendError(f"{cls} does not implement the Backend protocol")
    return backend
