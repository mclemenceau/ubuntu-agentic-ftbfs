"""Persist snapshots and keep the item inventory in sync with them.

An item is one failing build: (source, version, arch). Each snapshot is
diffed against the inventory:
  - new:    appears for the first time (or reappears after being gone)
  - active: was already there
  - gone:   no longer listed (fixed, superseded or removed)
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path

from .db import DB
from .ingest import Snapshot


def item_id(source: str, version: str, arch: str) -> str:
    return f"{source}/{version}/{arch}"


@dataclass
class Diff:
    snapshot_id: int
    new: list[str]
    gone: list[str]
    active: int


def save_snapshot(snap: Snapshot, snapshots_dir: Path) -> Path:
    snapshots_dir.mkdir(parents=True, exist_ok=True)
    stamp = snap.fetched_at.replace(":", "").replace("+0000", "Z")
    path = snapshots_dir / f"{snap.series}-{stamp}.json"
    path.write_text(json.dumps(snap.to_dict(), indent=1))
    return path


def store(db: DB, snap: Snapshot, path: Path) -> Diff:
    with db.transaction():
        sid = db.execute(
            "INSERT INTO snapshot (series, fetched_at, source_url, path)"
            " VALUES (?, ?, ?, ?)",
            (snap.series, snap.fetched_at, snap.source_url, str(path)),
        ).lastrowid
        before = {
            r["id"]: r["lifecycle"]
            for r in db.query("SELECT id, lifecycle FROM item")
        }
        seen: set[str] = set()
        new: list[str] = []
        for p in snap.packages:
            db.execute(
                "INSERT OR REPLACE INTO package VALUES (?,?,?,?,?,?,?,?)",
                (p.source, p.component, json.dumps(p.packagesets),
                 json.dumps(p.teams), json.dumps(p.lp_bugs), p.pts, p.bts,
                 sid),
            )
            for v in p.versions:
                for b in v.builds:
                    iid = item_id(p.source, v.version, b.arch)
                    seen.add(iid)
                    was = before.get(iid)
                    lifecycle = "active" if was in ("new", "active") else "new"
                    if lifecycle == "new":
                        new.append(iid)
                    db.execute(
                        """
                        INSERT INTO item VALUES
                          (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                        ON CONFLICT(id) DO UPDATE SET
                          pocket=excluded.pocket,
                          changed_by=excluded.changed_by,
                          state=excluded.state,
                          build_id=excluded.build_id,
                          build_url=excluded.build_url,
                          log_url=excluded.log_url,
                          finished_at=excluded.finished_at,
                          note=excluded.note,
                          lifecycle=excluded.lifecycle,
                          last_seen=excluded.last_seen
                        """,
                        (iid, p.source, v.version, b.arch, v.pocket,
                         v.changed_by, b.state, b.build_id, b.build_url,
                         b.log_url, b.finished_at, b.note, lifecycle,
                         sid, sid),
                    )
        gone = [i for i, lc in before.items()
                if i not in seen and lc != "gone"]
        for iid in gone:
            db.execute(
                "UPDATE item SET lifecycle='gone' WHERE id=?", (iid,)
            )
    db.event("snapshot", new=len(new), gone=len(gone),
             active=len(seen) - len(new), snapshot_id=sid, path=str(path))
    return Diff(snapshot_id=sid, new=new, gone=gone,
                active=len(seen) - len(new))


def latest_snapshot_path(db: DB) -> Path | None:
    row = db.one("SELECT path FROM snapshot ORDER BY id DESC LIMIT 1")
    return Path(row["path"]) if row else None
