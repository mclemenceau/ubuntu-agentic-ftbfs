# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Moving an instance to another directory (`ftbfs relocate OLD_ROOT`).

Results store absolute paths (build logs, patches, dsc files, debdiffs),
events and snapshots too, and work/ has symlinks into cache/. A
database copied under another root would point at files that are not
there. Rewriting those paths changes the results' data, which feeds the
inputs hash of the stages after them: a plain rewrite would re-run
reproduce, dev (tokens, and every gate asked again) and verify.

So relocate rewrites the old root in every text column and in the
symlinks under work/ and cache/, then moves each stored inputs hash
that was current onto the hash the rewritten data gives. What was
cached stays cached, what was stale stays stale: the next run plans the
same work as before the move.
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import TYPE_CHECKING

from .core.context import Units
from .filters import Filter, all_items

if TYPE_CHECKING:
    from .app import App
    from .db import DB


@dataclass
class Relocation:
    old: Path
    new: Path
    fields: dict[str, int] = field(default_factory=dict)  # per table
    hashes: int = 0  # stage results moved onto a new inputs hash
    links: int = 0  # symlinks retargeted
    dry_run: bool = False


class _DryRun(Exception):
    pass


def relocate(app: App, old: Path, dry_run: bool = False) -> Relocation:
    """Rewrite paths under `old` to the app's root. The database changes
    in one transaction; with dry_run, it is rolled back and no symlink
    changes."""
    new = app.config.root
    _check(app, old, new)
    rel = Relocation(old, new, dry_run=dry_run)
    selections = _selections(app)
    try:
        with app.db.transaction():
            before = _hashes(app, selections)
            rel.fields = _rewrite(app.db, f"{old}/", f"{new}/")
            after = _hashes(app, selections)
            moves = {(stage, uid, h0): after[i][stage, uid]
                     for i, hashes in enumerate(before)
                     for (stage, uid), h0 in hashes.items()
                     if after[i][stage, uid] != h0}
            for (stage, uid, h0), h1 in moves.items():
                rel.hashes += app.db.execute(
                    "UPDATE stage_result SET inputs_hash=? WHERE"
                    " stage=? AND unit_id=? AND inputs_hash=?",
                    (h1, stage, uid, h0)).rowcount
            if dry_run:
                raise _DryRun
    except _DryRun:
        pass
    for d in (app.config.work_dir, app.config.cache_dir):
        rel.links += _retarget(d, f"{old}/", f"{new}/", dry_run)
    return rel


def _check(app: App, old: Path, new: Path) -> None:
    if not old.is_absolute():
        raise ValueError(f"{old} is not an absolute path")
    if old == new:
        raise ValueError(f"{old} is already this instance's root")
    for p in (old, new):
        # Paths sit inside JSON text: a character JSON escapes would not
        # match there.
        if json.dumps(str(p), ensure_ascii=True)[1:-1] != str(p):
            raise ValueError(f"{p}: only plain ASCII paths can be"
                             " rewritten")
    app.reap_stale_runs()
    if app.db.one("SELECT 1 FROM run WHERE status='running'"):
        raise ValueError("a run is in progress: relocate between runs")


def _selections(app: App) -> list[Units]:
    """Every selection whose cache keys matter: all items, the default
    filter and the filters of past runs. A package stage that depends on
    an item stage hashes the selected items only, so its hash depends on
    the selection."""
    filters = [app.config.make_filter()]
    for r in app.db.query("SELECT DISTINCT filter FROM run"):
        try:
            filters.append(Filter.from_dict(json.loads(r["filter"])))
        except (ValueError, TypeError):
            continue  # an older filter format: no run uses it again
    item_sets = [all_items(app.db)] + [app.workable(f) for f in filters]
    seen, out = set(), []
    for items in item_sets:
        key = tuple(r["id"] for r in items)
        if key not in seen:
            seen.add(key)
            out.append(Units(items, app.db))
    return out


def _hashes(app: App, selections: list[Units]
            ) -> list[dict[tuple[str, str], str]]:
    """Per selection, the inputs hash each unit with a result would have
    now, per stage."""
    have = {(r["stage"], r["unit_id"]) for r in app.db.query(
        "SELECT DISTINCT stage, unit_id FROM stage_result")}
    out = []
    for units in selections:
        sched = app.scheduler(units)
        out.append({
            (spec.name, uid): sched.hash_now(spec, uid)
            for spec in app.pipeline
            for uid in units.of(spec.stage.unit)
            if (spec.name, uid) in have
        })
    return out


def _rewrite(db: DB, old: str, new: str) -> dict[str, int]:
    out = {}
    tables = [r["name"] for r in db.query(
        "SELECT name FROM sqlite_master WHERE type='table'"
        " AND name NOT LIKE 'sqlite_%' ORDER BY name")]
    for t in tables:
        n = 0
        for col in db.query(f'PRAGMA table_info("{t}")'):
            if col["type"].upper() != "TEXT":
                continue
            c = col["name"]
            n += db.execute(
                f'UPDATE "{t}" SET "{c}" = replace("{c}", ?, ?)'
                f' WHERE instr("{c}", ?) > 0', (old, new, old)).rowcount
        if n:
            out[t] = n
    return out


def _retarget(top: Path, old: str, new: str, dry_run: bool) -> int:
    n = 0
    for d, dirs, files in os.walk(top):
        for name in dirs + files:
            p = Path(d, name)
            if not p.is_symlink():
                continue
            target = os.readlink(p)
            if not target.startswith(old):
                continue
            n += 1
            if not dry_run:
                p.unlink()
                p.symlink_to(new + target[len(old):])
    return n
