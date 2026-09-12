"""The backends: Frama-C/WP (default), RefinedC, and the null backend used by tests."""

from __future__ import annotations

from pathlib import Path
from typing import Any

from fver.backends.base import Backend, BackendError
from fver.core.models import Target


def make_backend(
    name: str, workspace_dir: Path, settings: dict[str, Any], target: Target
) -> Backend:
    if name == "framac":
        from fver.backends.framac.backend import FramaCBackend

        return FramaCBackend(workspace_dir=workspace_dir, settings=settings, target=target)
    if name == "refinedc":
        from fver.backends.refinedc.backend import RefinedCBackend

        return RefinedCBackend(workspace_dir=workspace_dir, settings=settings, target=target)
    if name == "null":
        from fver.backends.null import NullBackend

        return NullBackend(workspace_dir=workspace_dir, settings=settings, target=target)
    raise BackendError(f"Unknown backend '{name}'. Available: framac, refinedc, null")
