"""SQLite implementation of the Ledger protocol.

This is the only module in fver that imports sqlite3. One connection per
thread (WAL journal, busy timeout) so the proof loop can record claims from
worker threads without coordination.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from dataclasses import asdict
from pathlib import Path
from typing import Any

from fver.core.models import (
    Claim,
    Cost,
    Finding,
    FunctionInfo,
    PropertyClass,
    Status,
    TranslationUnit,
    now_iso,
)
from fver.ledger.api import FunctionRow, Summary
from fver.ledger.memory import derive_status, new_run_id, summarize_rows, unsupported_claim

# Each entry is one migration; the schema_version table records how many
# have been applied. Append, never edit, once released.
MIGRATIONS: list[str] = [
    """
    CREATE TABLE IF NOT EXISTS runs (
        id TEXT PRIMARY KEY,
        command TEXT NOT NULL,
        backend TEXT NOT NULL,
        target_key TEXT NOT NULL,
        started_at TEXT NOT NULL,
        ended_at TEXT,
        ok INTEGER,
        message TEXT NOT NULL DEFAULT '',
        meta_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE TABLE IF NOT EXISTS tus (
        id TEXT PRIMARY KEY,
        source_path TEXT NOT NULL,
        directory TEXT NOT NULL,
        arguments_json TEXT NOT NULL,
        preprocessed_path TEXT
    );
    CREATE TABLE IF NOT EXISTS functions (
        id TEXT PRIMARY KEY,
        name TEXT NOT NULL,
        tu_id TEXT NOT NULL,
        source_path TEXT NOT NULL,
        start_line INTEGER NOT NULL,
        end_line INTEGER NOT NULL,
        signature TEXT NOT NULL,
        body_hash TEXT NOT NULL,
        is_static INTEGER NOT NULL DEFAULT 0,
        callees_json TEXT NOT NULL DEFAULT '[]',
        attack_score REAL NOT NULL DEFAULT 0,
        attack_reasons_json TEXT NOT NULL DEFAULT '[]',
        updated_at TEXT NOT NULL
    );
    CREATE INDEX IF NOT EXISTS functions_name ON functions(name);
    CREATE INDEX IF NOT EXISTS functions_source ON functions(source_path);
    CREATE TABLE IF NOT EXISTS calls (
        caller_id TEXT NOT NULL REFERENCES functions(id) ON DELETE CASCADE,
        callee_name TEXT NOT NULL,
        PRIMARY KEY (caller_id, callee_name)
    );
    CREATE INDEX IF NOT EXISTS calls_callee ON calls(callee_name);
    CREATE TABLE IF NOT EXISTS claims (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        function_id TEXT NOT NULL,
        property_class TEXT NOT NULL,
        backend TEXT NOT NULL,
        target_key TEXT NOT NULL,
        status TEXT NOT NULL,
        body_hash TEXT NOT NULL,
        cache_key TEXT NOT NULL,
        proof_hash TEXT,
        tool_versions_json TEXT NOT NULL DEFAULT '{}',
        assumptions_json TEXT NOT NULL DEFAULT '[]',
        message TEXT NOT NULL DEFAULT '',
        cost_json TEXT NOT NULL DEFAULT '{}',
        created_at TEXT NOT NULL,
        run_id TEXT NOT NULL DEFAULT '',
        extra_json TEXT NOT NULL DEFAULT '{}'
    );
    CREATE INDEX IF NOT EXISTS claims_lookup
        ON claims(function_id, backend, target_key, created_at);
    CREATE TABLE IF NOT EXISTS findings (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        function_id TEXT,
        source_path TEXT NOT NULL,
        line INTEGER,
        kind TEXT NOT NULL,
        tool TEXT NOT NULL,
        message TEXT NOT NULL,
        witness TEXT NOT NULL DEFAULT '',
        created_at TEXT NOT NULL,
        run_id TEXT NOT NULL DEFAULT ''
    );
    CREATE INDEX IF NOT EXISTS findings_function ON findings(function_id);
    CREATE INDEX IF NOT EXISTS findings_source ON findings(source_path);
    """,
    """
    ALTER TABLE findings ADD COLUMN confidence TEXT NOT NULL DEFAULT 'high';
    """,
]


def _dumps(obj: Any) -> str:
    return json.dumps(obj, sort_keys=True)


class SqliteLedger:
    """Ledger backed by a SQLite file. See fver.ledger.api.Ledger."""

    def __init__(self, path: Path | str):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._local = threading.local()
        self._all_conns: list[sqlite3.Connection] = []
        self._conns_lock = threading.Lock()
        self._migrate()

    # -- connections -----------------------------------------------------------

    @property
    def _conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.path), timeout=30.0, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA journal_mode=WAL")
            conn.execute("PRAGMA foreign_keys=ON")
            conn.execute("PRAGMA busy_timeout=30000")
            self._local.conn = conn
            with self._conns_lock:
                self._all_conns.append(conn)
        return conn

    def _migrate(self) -> None:
        conn = self._conn
        conn.execute("CREATE TABLE IF NOT EXISTS schema_version (version INTEGER NOT NULL)")
        row = conn.execute("SELECT version FROM schema_version").fetchone()
        current = row["version"] if row else 0
        for i in range(current, len(MIGRATIONS)):
            # executescript commits any open transaction itself; run the
            # migration then bump the version.
            conn.executescript(MIGRATIONS[i])
            if row is None and i == 0:
                conn.execute("INSERT INTO schema_version(version) VALUES (?)", (i + 1,))
            else:
                conn.execute("UPDATE schema_version SET version = ?", (i + 1,))

    def close(self) -> None:
        with self._conns_lock:
            for c in self._all_conns:
                try:
                    c.close()
                except sqlite3.ProgrammingError:
                    pass
            self._all_conns.clear()
        self._local.conn = None

    # -- runs ------------------------------------------------------------------

    def start_run(self, command: str, backend: str, target_key: str, meta: dict) -> str:
        run_id = new_run_id()
        self._conn.execute(
            "INSERT INTO runs(id, command, backend, target_key, started_at, meta_json)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, command, backend, target_key, now_iso(), _dumps(meta)),
        )
        return run_id

    def end_run(self, run_id: str, ok: bool, message: str = "") -> None:
        self._conn.execute(
            "UPDATE runs SET ended_at = ?, ok = ?, message = ? WHERE id = ?",
            (now_iso(), 1 if ok else 0, message, run_id),
        )

    # -- build index -----------------------------------------------------------

    def upsert_tus(self, tus: Iterable[TranslationUnit]) -> None:
        conn = self._conn
        conn.execute("BEGIN")
        try:
            conn.executemany(
                "INSERT INTO tus(id, source_path, directory, arguments_json, preprocessed_path)"
                " VALUES (?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET source_path=excluded.source_path,"
                " directory=excluded.directory, arguments_json=excluded.arguments_json,"
                " preprocessed_path=excluded.preprocessed_path",
                [
                    (
                        tu.id,
                        tu.source_path,
                        tu.directory,
                        _dumps(tu.arguments),
                        tu.preprocessed_path,
                    )
                    for tu in tus
                ],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def prune(self, keep_tu_ids: set[str], keep_function_ids: set[str]) -> int:
        conn = self._conn
        conn.execute("BEGIN")
        try:
            rows = conn.execute("SELECT id FROM functions").fetchall()
            gone = [r["id"] for r in rows if r["id"] not in keep_function_ids]
            for i in range(0, len(gone), 500):
                chunk = gone[i : i + 500]
                marks = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM calls WHERE caller_id IN ({marks})", chunk)
                conn.execute(f"DELETE FROM functions WHERE id IN ({marks})", chunk)
            tus = [r["id"] for r in conn.execute("SELECT id FROM tus").fetchall()]
            gone_tus = [t for t in tus if t not in keep_tu_ids]
            for i in range(0, len(gone_tus), 500):
                chunk = gone_tus[i : i + 500]
                marks = ",".join("?" * len(chunk))
                conn.execute(f"DELETE FROM tus WHERE id IN ({marks})", chunk)
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise
        return len(gone)

    def upsert_functions(self, functions: Iterable[FunctionInfo]) -> None:
        conn = self._conn
        fns = list(functions)
        conn.execute("BEGIN")
        try:
            ts = now_iso()
            conn.executemany(
                "INSERT INTO functions(id, name, tu_id, source_path, start_line, end_line,"
                " signature, body_hash, is_static, callees_json, attack_score,"
                " attack_reasons_json, updated_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(id) DO UPDATE SET name=excluded.name, tu_id=excluded.tu_id,"
                " source_path=excluded.source_path, start_line=excluded.start_line,"
                " end_line=excluded.end_line, signature=excluded.signature,"
                " body_hash=excluded.body_hash, is_static=excluded.is_static,"
                " callees_json=excluded.callees_json, attack_score=excluded.attack_score,"
                " attack_reasons_json=excluded.attack_reasons_json, updated_at=excluded.updated_at",
                [
                    (
                        f.id,
                        f.name,
                        f.tu_id,
                        f.source_path,
                        f.start_line,
                        f.end_line,
                        f.signature,
                        f.body_hash,
                        1 if f.is_static else 0,
                        _dumps(f.callees),
                        f.attack_score,
                        _dumps(f.attack_reasons),
                        ts,
                    )
                    for f in fns
                ],
            )
            conn.executemany("DELETE FROM calls WHERE caller_id = ?", [(f.id,) for f in fns])
            conn.executemany(
                "INSERT OR IGNORE INTO calls(caller_id, callee_name) VALUES (?, ?)",
                [(f.id, c) for f in fns for c in f.callees],
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    def get_tu(self, tu_id: str) -> TranslationUnit | None:
        row = self._conn.execute("SELECT * FROM tus WHERE id = ?", (tu_id,)).fetchone()
        return _tu_from_row(row) if row else None

    def get_function(self, function_id: str) -> FunctionInfo | None:
        row = self._conn.execute("SELECT * FROM functions WHERE id = ?", (function_id,)).fetchone()
        return _function_from_row(row) if row else None

    def find_functions(
        self, name: str | None = None, source_path: str | None = None
    ) -> list[FunctionInfo]:
        clauses, params = [], []
        if name is not None:
            clauses.append("name = ?")
            params.append(name)
        if source_path is not None:
            clauses.append("source_path = ?")
            params.append(source_path)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM functions {where} ORDER BY source_path, start_line", params
        ).fetchall()
        return [_function_from_row(r) for r in rows]

    def list_functions(
        self,
        backend: str,
        target_key: str,
        status: Status | None = None,
        order_by_attack_score: bool = True,
        limit: int | None = None,
    ) -> list[FunctionRow]:
        order = (
            "ORDER BY f.attack_score DESC, f.source_path, f.start_line"
            if order_by_attack_score
            else "ORDER BY f.source_path, f.start_line"
        )
        # Latest claim per function for this backend/target: highest created_at,
        # ties broken by the autoincrement id.
        sql = f"""
            SELECT f.*, c.id AS claim_id
            FROM functions f
            LEFT JOIN claims c ON c.id = (
                SELECT c2.id FROM claims c2
                WHERE c2.function_id = f.id AND c2.backend = ? AND c2.target_key = ?
                ORDER BY c2.created_at DESC, c2.id DESC LIMIT 1
            )
            {order}
        """
        rows = self._conn.execute(sql, (backend, target_key)).fetchall()
        claim_ids = [r["claim_id"] for r in rows if r["claim_id"] is not None]
        claims = self._claims_by_id(claim_ids)
        out: list[FunctionRow] = []
        for r in rows:
            claim = claims.get(r["claim_id"]) if r["claim_id"] is not None else None
            fn = _function_from_row(r)
            st = derive_status(fn, claim)
            if status is not None and st is not status:
                continue
            out.append(FunctionRow(function=fn, status=st, claim=claim))
            if limit is not None and len(out) >= limit:
                break
        return out

    def _claims_by_id(self, ids: list[int]) -> dict[int, Claim]:
        out: dict[int, Claim] = {}
        conn = self._conn
        for i in range(0, len(ids), 500):
            chunk = ids[i : i + 500]
            marks = ",".join("?" * len(chunk))
            for row in conn.execute(f"SELECT * FROM claims WHERE id IN ({marks})", chunk):
                out[row["id"]] = _claim_from_row(row)
        return out

    # -- claims ----------------------------------------------------------------

    def record_claim(self, claim: Claim) -> None:
        self._conn.execute(
            "INSERT INTO claims(function_id, property_class, backend, target_key, status,"
            " body_hash, cache_key, proof_hash, tool_versions_json, assumptions_json, message,"
            " cost_json, created_at, run_id, extra_json)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                claim.function_id,
                claim.property_class.value,
                claim.backend,
                claim.target_key,
                claim.status.value,
                claim.body_hash,
                claim.cache_key,
                claim.proof_hash,
                _dumps(claim.tool_versions),
                _dumps(claim.assumptions),
                claim.message,
                _dumps(asdict(claim.cost)),
                claim.created_at,
                claim.run_id,
                _dumps(claim.extra),
            ),
        )

    def current_claim(self, function_id: str, backend: str, target_key: str) -> Claim | None:
        row = self._conn.execute(
            "SELECT * FROM claims WHERE function_id = ? AND backend = ? AND target_key = ?"
            " ORDER BY created_at DESC, id DESC LIMIT 1",
            (function_id, backend, target_key),
        ).fetchone()
        return _claim_from_row(row) if row else None

    def claims_for(self, function_id: str) -> list[Claim]:
        rows = self._conn.execute(
            "SELECT * FROM claims WHERE function_id = ? ORDER BY created_at, id", (function_id,)
        ).fetchall()
        return [_claim_from_row(r) for r in rows]

    def mark_unsupported(
        self, function_id: str, backend: str, target_key: str, reason: str, run_id: str
    ) -> None:
        fn = self.get_function(function_id)
        if fn is None:
            raise KeyError(f"unknown function {function_id}")
        self.record_claim(unsupported_claim(fn, backend, target_key, reason, run_id))

    # -- findings --------------------------------------------------------------

    def record_finding(self, finding: Finding) -> None:
        self._conn.execute(
            "INSERT INTO findings(function_id, source_path, line, kind, tool, message, witness,"
            " confidence, created_at, run_id) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                finding.function_id,
                finding.source_path,
                finding.line,
                finding.kind,
                finding.tool,
                finding.message,
                finding.witness,
                finding.confidence,
                finding.created_at,
                finding.run_id,
            ),
        )

    def delete_findings(self, source_paths: set[str], tool: str | None = None) -> int:
        paths = list(source_paths)
        if not paths:
            return 0
        total = 0
        conn = self._conn
        for i in range(0, len(paths), 500):
            chunk = paths[i : i + 500]
            marks = ",".join("?" * len(chunk))
            sql = f"DELETE FROM findings WHERE source_path IN ({marks})"
            params: list = list(chunk)
            if tool is not None:
                sql += " AND tool = ?"
                params.append(tool)
            total += conn.execute(sql, params).rowcount
        if conn.in_transaction:
            conn.commit()
        return total

    def findings(
        self, function_id: str | None = None, source_path: str | None = None
    ) -> list[Finding]:
        clauses, params = [], []
        if function_id is not None:
            clauses.append("function_id = ?")
            params.append(function_id)
        if source_path is not None:
            clauses.append("source_path = ?")
            params.append(source_path)
        where = ("WHERE " + " AND ".join(clauses)) if clauses else ""
        rows = self._conn.execute(
            f"SELECT * FROM findings {where} ORDER BY created_at, id", params
        ).fetchall()
        return [_finding_from_row(r) for r in rows]

    # -- reporting -------------------------------------------------------------

    def summary(self, backend: str, target_key: str) -> Summary:
        rows = self.list_functions(backend, target_key, order_by_attack_score=False)
        n = self._conn.execute("SELECT COUNT(*) AS n FROM findings").fetchone()["n"]
        return summarize_rows(rows, int(n))

    def dependents(self, function_name: str) -> list[FunctionInfo]:
        rows = self._conn.execute(
            "SELECT f.* FROM functions f JOIN calls c ON c.caller_id = f.id"
            " WHERE c.callee_name = ? ORDER BY f.source_path, f.start_line",
            (function_name,),
        ).fetchall()
        return [_function_from_row(r) for r in rows]


# -- row converters --------------------------------------------------------------


def _tu_from_row(r: sqlite3.Row) -> TranslationUnit:
    return TranslationUnit(
        id=r["id"],
        source_path=r["source_path"],
        directory=r["directory"],
        arguments=json.loads(r["arguments_json"]),
        preprocessed_path=r["preprocessed_path"],
    )


def _function_from_row(r: sqlite3.Row) -> FunctionInfo:
    return FunctionInfo(
        id=r["id"],
        name=r["name"],
        tu_id=r["tu_id"],
        source_path=r["source_path"],
        start_line=r["start_line"],
        end_line=r["end_line"],
        signature=r["signature"],
        body_hash=r["body_hash"],
        is_static=bool(r["is_static"]),
        callees=json.loads(r["callees_json"]),
        attack_score=float(r["attack_score"]),
        attack_reasons=json.loads(r["attack_reasons_json"]),
    )


def _claim_from_row(r: sqlite3.Row) -> Claim:
    return Claim(
        function_id=r["function_id"],
        property_class=PropertyClass(r["property_class"]),
        backend=r["backend"],
        target_key=r["target_key"],
        status=Status(r["status"]),
        body_hash=r["body_hash"],
        cache_key=r["cache_key"],
        proof_hash=r["proof_hash"],
        tool_versions=json.loads(r["tool_versions_json"]),
        assumptions=json.loads(r["assumptions_json"]),
        message=r["message"],
        cost=Cost(**json.loads(r["cost_json"])),
        created_at=r["created_at"],
        run_id=r["run_id"],
        extra=json.loads(r["extra_json"]),
    )


def _finding_from_row(r: sqlite3.Row) -> Finding:
    return Finding(
        function_id=r["function_id"],
        source_path=r["source_path"],
        line=r["line"],
        kind=r["kind"],
        tool=r["tool"],
        message=r["message"],
        witness=r["witness"],
        confidence=r["confidence"] if "confidence" in r.keys() else "high",
        created_at=r["created_at"],
        run_id=r["run_id"],
    )
