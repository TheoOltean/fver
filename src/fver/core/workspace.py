"""The .fver/ directory: everything fver writes lives here.

fver never modifies the user's source files. Annotated copies, generated
proof files, the ledger, caches and logs are all under <repo>/.fver/.

Layout
------
.fver/
  config.toml            project configuration (committed)
  ledger.sqlite          verification ledger (committed or not; user's choice)
  proofs/<src path>/<function>/   accepted submissions + audit results (committed)
  external/              trusted specs for libc / external functions (committed)
  work/                  build capture, preprocessed TUs, function index (ignored)
  backend/<name>/        the backend's private project directory (ignored)
  cache/                 content-addressed results (ignored)
  logs/                  run logs, LLM transcripts (ignored)
"""

from __future__ import annotations

import json
import os
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

from fver.core.config import CONFIG_DIR_NAME, CONFIG_FILE_NAME, FverConfig, load_config

GITIGNORE_BODY = """# Managed by fver. Everything here is derived state except config.toml,
# ledger.sqlite, proofs/ and external/.
work/
backend/
cache/
logs/
"""


class NotInitialised(RuntimeError):
    pass


def find_repo_root(start: Path | None = None) -> Path | None:
    """Walk up from `start` looking for a .fver directory."""
    p = (start or Path.cwd()).resolve()
    for candidate in [p, *p.parents]:
        if (candidate / CONFIG_DIR_NAME / CONFIG_FILE_NAME).exists():
            return candidate
    return None


class Workspace:
    def __init__(self, repo_root: Path, config: FverConfig | None = None):
        self.repo_root = repo_root.resolve()
        self.root = self.repo_root / CONFIG_DIR_NAME
        self.config = config or load_config(self.repo_root)

    # -- construction -------------------------------------------------------

    @classmethod
    def open(cls, start: Path | None = None) -> Workspace:
        root = find_repo_root(start)
        if root is None:
            raise NotInitialised(
                "No .fver/config.toml found here or in any parent directory. Run `fver init`."
            )
        return cls(root)

    @classmethod
    def create(cls, repo_root: Path, config: FverConfig) -> Workspace:
        ws = cls(repo_root, config)
        for d in (
            ws.proofs_dir,
            ws.external_dir,
            ws.work_dir,
            ws.backend_root,
            ws.cache_dir,
            ws.logs_dir,
        ):
            d.mkdir(parents=True, exist_ok=True)
        (ws.root / ".gitignore").write_text(GITIGNORE_BODY, encoding="utf-8")
        return ws

    # -- paths ----------------------------------------------------------------

    @property
    def config_path(self) -> Path:
        return self.root / CONFIG_FILE_NAME

    @property
    def ledger_path(self) -> Path:
        return self.root / "ledger.sqlite"

    @property
    def proofs_dir(self) -> Path:
        return self.root / "proofs"

    @property
    def external_dir(self) -> Path:
        return self.root / "external"

    @property
    def work_dir(self) -> Path:
        return self.root / "work"

    @property
    def backend_root(self) -> Path:
        return self.root / "backend"

    def backend_dir(self, name: str) -> Path:
        d = self.backend_root / name
        d.mkdir(parents=True, exist_ok=True)
        return d

    @property
    def cache_dir(self) -> Path:
        return self.root / "cache"

    @property
    def logs_dir(self) -> Path:
        return self.root / "logs"

    def proof_dir(self, source_path: str, function_name: str) -> Path:
        d = self.proofs_dir / source_path / function_name
        d.mkdir(parents=True, exist_ok=True)
        return d

    def tu_work_dir(self, tu_id: str) -> Path:
        d = self.work_dir / "tu" / tu_id
        d.mkdir(parents=True, exist_ok=True)
        return d

    # -- small JSON state files under work/ ------------------------------------

    def state_path(self, name: str) -> Path:
        return self.work_dir / f"{name}.json"

    def write_state(self, name: str, obj: Any) -> Path:
        p = self.state_path(name)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(_jsonable(obj), indent=2, sort_keys=True), encoding="utf-8")
        return p

    def read_state(self, name: str, default: Any = None) -> Any:
        p = self.state_path(name)
        if not p.exists():
            return default
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))

    # -- helpers ----------------------------------------------------------------

    def is_inside_workspace(self, path: Path) -> bool:
        try:
            path.resolve().relative_to(self.root)
            return True
        except ValueError:
            return False

    def assert_not_user_file(self, path: Path) -> None:
        """Guard used by anything that writes: refuse to touch files outside .fver/."""
        if not self.is_inside_workspace(path):
            raise PermissionError(f"fver refuses to write outside {self.root}: {path}")

    def relpath(self, path: Path) -> str:
        return os.path.relpath(path.resolve(), self.repo_root).replace(os.sep, "/")


def _jsonable(obj: Any) -> Any:
    if is_dataclass(obj) and not isinstance(obj, type):
        return _jsonable(asdict(obj))
    if isinstance(obj, dict):
        return {k: _jsonable(v) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_jsonable(v) for v in obj]
    if isinstance(obj, Path):
        return str(obj)
    if hasattr(obj, "value") and hasattr(type(obj), "__members__"):
        return obj.value
    return obj
