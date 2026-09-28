"""SQLite store: the single source of truth shared by runner, CLI and UI.

WAL mode lets the web UI read while a run writes. One connection is shared
by the runner's worker threads; writes are serialized with a lock.
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterable
from datetime import UTC, datetime
from pathlib import Path

SCHEMA = [
    # v1: core
    """
    CREATE TABLE snapshot (
        id INTEGER PRIMARY KEY,
        series TEXT NOT NULL,
        fetched_at TEXT NOT NULL,
        source_url TEXT NOT NULL,
        path TEXT NOT NULL
    );
    CREATE TABLE package (
        source TEXT PRIMARY KEY,
        component TEXT NOT NULL,
        packagesets TEXT NOT NULL,
        teams TEXT NOT NULL,
        lp_bugs TEXT NOT NULL,
        pts TEXT,
        bts TEXT,
        snapshot_id INTEGER NOT NULL
    );
    CREATE TABLE item (
        id TEXT PRIMARY KEY,
        source TEXT NOT NULL,
        version TEXT NOT NULL,
        arch TEXT NOT NULL,
        pocket TEXT NOT NULL,
        changed_by TEXT,
        state TEXT NOT NULL,
        build_id INTEGER NOT NULL,
        build_url TEXT NOT NULL,
        log_url TEXT,
        finished_at TEXT,
        note TEXT,
        lifecycle TEXT NOT NULL,
        first_seen INTEGER NOT NULL,
        last_seen INTEGER NOT NULL
    );
    CREATE INDEX item_source ON item(source);
    CREATE TABLE run (
        id INTEGER PRIMARY KEY,
        started TEXT NOT NULL,
        finished TEXT,
        status TEXT NOT NULL,
        control TEXT,
        filter TEXT NOT NULL,
        pipeline_hash TEXT NOT NULL,
        trigger TEXT NOT NULL
    );
    CREATE TABLE event (
        id INTEGER PRIMARY KEY,
        run_id INTEGER,
        ts TEXT NOT NULL,
        unit TEXT,
        stage TEXT,
        type TEXT NOT NULL,
        payload TEXT NOT NULL
    );
    CREATE INDEX event_unit ON event(unit, stage);
    CREATE INDEX event_run ON event(run_id);
    CREATE TABLE stage_result (
        id INTEGER PRIMARY KEY,
        run_id INTEGER,
        unit_type TEXT NOT NULL,
        unit_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        stage_version TEXT NOT NULL,
        inputs_hash TEXT NOT NULL,
        attempt INTEGER NOT NULL,
        status TEXT NOT NULL,
        data TEXT NOT NULL,
        artifacts TEXT NOT NULL,
        backend TEXT,
        model TEXT,
        usage TEXT,
        cost REAL,
        ts TEXT NOT NULL
    );
    CREATE INDEX stage_result_unit ON stage_result(unit_id, stage);
    CREATE TABLE gate (
        unit_id TEXT NOT NULL,
        stage TEXT NOT NULL,
        decision TEXT NOT NULL,
        by TEXT,
        note TEXT,
        ts TEXT NOT NULL,
        PRIMARY KEY (unit_id, stage)
    );
    """,
    # v2: classification results denormalized onto items for grouping
    """
    ALTER TABLE item ADD COLUMN cluster_id TEXT;
    ALTER TABLE item ADD COLUMN class TEXT;
    ALTER TABLE item ADD COLUMN family TEXT;
    CREATE INDEX item_cluster ON item(cluster_id);
    """,
    # v3: which process owns a run, to detect runs that died
    """
    ALTER TABLE run ADD COLUMN pid INTEGER;
    ALTER TABLE run ADD COLUMN host TEXT;
    """,
    # v4: what a human decided about a source version, outside the
    # pipeline (fix accepted, uploaded, won't fix, handled elsewhere)
    """
    CREATE TABLE disposition (
        source TEXT NOT NULL,
        version TEXT NOT NULL,
        status TEXT NOT NULL,
        by TEXT,
        note TEXT,
        ts TEXT NOT NULL,
        PRIMARY KEY (source, version)
    );
    """,
]


def live(col: str) -> str:
    """SQL predicate: the unit id in `col` (an item, source or cluster)
    still has an item listed as failing. Results and gates of gone units
    stay recorded but no longer ask anything of a human."""
    return ("EXISTS (SELECT 1 FROM item WHERE lifecycle != 'gone' AND"
            f" {col} IN (item.id, item.source, item.cluster_id))")


# Gates waiting for a decision, on units that are still failing.
PENDING_GATES = (
    "SELECT * FROM gate WHERE decision='pending' AND "
    + live("gate.unit_id"))


def now() -> str:
    return datetime.now(UTC).isoformat(timespec="seconds")


class DB:
    def __init__(self, path: Path | str):
        if str(path) != ":memory:":
            Path(path).parent.mkdir(parents=True, exist_ok=True)
        self.conn = sqlite3.connect(
            str(path), check_same_thread=False, isolation_level=None
        )
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA busy_timeout=5000")
        self.lock = threading.RLock()
        self._migrate()

    def _migrate(self) -> None:
        with self.lock:
            version = self.conn.execute("PRAGMA user_version").fetchone()[0]
            for i, script in enumerate(SCHEMA[version:], start=version + 1):
                self.conn.executescript(
                    f"BEGIN; {script}; PRAGMA user_version={i}; COMMIT;"
                )

    def execute(self, sql: str, params: Iterable = ()) -> sqlite3.Cursor:
        with self.lock:
            return self.conn.execute(sql, tuple(params))

    def query(self, sql: str, params: Iterable = ()) -> list[sqlite3.Row]:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchall()

    def one(self, sql: str, params: Iterable = ()) -> sqlite3.Row | None:
        with self.lock:
            return self.conn.execute(sql, tuple(params)).fetchone()

    def transaction(self):
        return _Tx(self)

    # -- events -----------------------------------------------------------

    def event(self, type_: str, *, run_id: int | None = None,
              unit: str | None = None, stage: str | None = None,
              **payload) -> int:
        cur = self.execute(
            "INSERT INTO event (run_id, ts, unit, stage, type, payload)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (run_id, now(), unit, stage, type_, json.dumps(payload)),
        )
        return cur.lastrowid


class _Tx:
    def __init__(self, db: DB):
        self.db = db

    def __enter__(self):
        self.db.lock.acquire()
        self.db.conn.execute("BEGIN")
        return self.db

    def __exit__(self, exc_type, *_):
        try:
            self.db.conn.execute("ROLLBACK" if exc_type else "COMMIT")
        finally:
            self.db.lock.release()
