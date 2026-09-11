"""Where accepted submissions and cached results live on disk.

.fver/proofs/<source path>/<function>/<submission files>
.fver/proofs/<source path>/<function>/result.json
.fver/proofs/<source path>/<function>/attempts/<n>/...   (best partial work)
.fver/cache/<cache_key>.json
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

from fver.backends.base import Backend, CheckResult, FunctionTask, Submission
from fver.core.models import FunctionInfo, Status, now_iso, sha256_text
from fver.core.workspace import Workspace
from fver.ledger.api import Ledger

RESULT_FILE = "result.json"


def _write_files(dir_: Path, files: dict[str, str]) -> None:
    dir_.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        safe = Path(name).name
        (dir_ / safe).write_text(content, encoding="utf-8")


def save_accepted(
    ws: Workspace,
    task: FunctionTask,
    submission: Submission,
    result: CheckResult,
    cache_key: str,
    backend_name: str,
) -> Path:
    d = ws.proof_dir(task.function.source_path, task.function.name)
    ws.assert_not_user_file(d)
    # Remove stale files from an older accepted submission.
    for old in d.iterdir():
        if old.is_file() and old.name != RESULT_FILE:
            old.unlink()
    _write_files(d, submission.files)
    record = {
        "function_id": task.function.id,
        "function": task.function.name,
        "source_path": task.function.source_path,
        "body_hash": task.function.body_hash,
        "backend": backend_name,
        "target": task.target.key,
        "cache_key": cache_key,
        "proof_hash": result.proof_hash,
        "assumptions": result.assumptions,
        "tool_versions": result.tool_versions,
        "outcome": result.outcome.value,
        "artifacts": result.artifacts,
        "files": sorted(submission.files),
        "note": submission.note,
        "accepted_at": now_iso(),
    }
    (d / RESULT_FILE).write_text(json.dumps(record, indent=2, sort_keys=True), encoding="utf-8")
    return d


def save_attempt(
    ws: Workspace, task: FunctionTask, attempt_no: int, submission: Submission, feedback: str
) -> Path:
    d = ws.proof_dir(task.function.source_path, task.function.name) / "attempts" / str(attempt_no)
    _write_files(d, submission.files)
    (d / "feedback.txt").write_text(feedback, encoding="utf-8")
    return d


def load_accepted(ws: Workspace, source_path: str, name: str) -> tuple[Submission, dict] | None:
    d = ws.proofs_dir / source_path / name
    rf = d / RESULT_FILE
    if not rf.exists():
        return None
    record = json.loads(rf.read_text(encoding="utf-8", errors="replace"))
    files = {}
    for fname in record.get("files", []):
        p = d / fname
        if p.exists():
            files[fname] = p.read_text(encoding="utf-8", errors="replace")
    if not files:
        return None
    return Submission(files=files, note=record.get("note", "")), record


# -- cache --------------------------------------------------------------------


def cache_path(ws: Workspace, cache_key: str) -> Path:
    return ws.cache_dir / f"{cache_key}.json"


def cache_lookup(ws: Workspace, cache_key: str) -> dict[str, Any] | None:
    p = cache_path(ws, cache_key)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8", errors="replace"))
    except json.JSONDecodeError:
        return None


def cache_insert(
    ws: Workspace,
    cache_key: str,
    status: Status,
    submission: Submission,
    result: CheckResult,
    message: str = "",
) -> None:
    ws.cache_dir.mkdir(parents=True, exist_ok=True)
    record = {
        "cache_key": cache_key,
        "status": status.value,
        "proof_hash": result.proof_hash,
        "assumptions": result.assumptions,
        "tool_versions": result.tool_versions,
        "submission": submission.files,
        "note": submission.note,
        "message": message,
        "created_at": now_iso(),
    }
    cache_path(ws, cache_key).write_text(
        json.dumps(record, indent=2, sort_keys=True), encoding="utf-8"
    )


# -- specs of callees / externals -------------------------------------------------


def callee_specs(
    ws: Workspace, ledger: Ledger, backend: Backend, function: FunctionInfo, target_key: str
) -> dict[str, str]:
    """Contracts of callees that have an accepted submission, in the backend's syntax.

    A stored accepted submission is the contract callers were (or will be)
    checked against. It stays in force while the callee is stale: if the
    callee is later re-verified with a different contract, the callers are
    invalidated then (see fver.agent.invalidate); if it is not re-verified,
    the caller's claim carries an explicit unverified-callee assumption.
    """
    out: dict[str, str] = {}
    for callee in function.callees:
        if callee == function.name:
            continue
        for cand in ledger.find_functions(name=callee):
            loaded = load_accepted(ws, cand.source_path, cand.name)
            if loaded is None:
                continue
            sub, _ = loaded
            spec = backend.extract_spec(sub, cand)
            if spec.strip():
                out[callee] = spec
                break
    return out


def unverified_callee_assumptions(
    ledger: Ledger, backend: Backend, function: FunctionInfo, target_key: str
) -> list[str]:
    """For each callee contract in use whose callee is not currently verified,
    an assumption string the claim must carry."""
    from fver.ledger.memory import derive_status

    out: list[str] = []
    for callee in function.callees:
        if callee == function.name:
            continue
        for cand in ledger.find_functions(name=callee):
            claim = ledger.current_claim(cand.id, backend.name, target_key)
            if claim is None:
                continue
            if derive_status(cand, claim) is not Status.VERIFIED:
                out.append(f"callee-contract-unverified:{callee}")
            break
    return out


def external_specs(ws: Workspace, function: FunctionInfo) -> dict[str, str]:
    """Trusted specs from .fver/external/<name>.<ext>, for callees that have one."""
    out: dict[str, str] = {}
    if not ws.external_dir.exists():
        return out
    for callee in function.callees:
        for p in sorted(ws.external_dir.glob(f"{callee}.*")):
            if p.is_file():
                out[callee] = p.read_text(encoding="utf-8", errors="replace")
                break
    return out


def hash_specs(specs: dict[str, str]) -> dict[str, str]:
    return {k: sha256_text(v)[:16] for k, v in specs.items()}
