# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""ftbfs relocate: a project moved to another directory keeps working
and keeps its cache."""

import json
import shutil
from pathlib import Path

import pytest

from ftbfs.app import App
from ftbfs.cli import main
from ftbfs.core.stage import (
    Kind,
    Stage,
    StageResult,
    Status,
    UnitType,
    register,
)
from ftbfs.filters import Filter
from ftbfs.inventory import store
from ftbfs.relocate import relocate

CALLS: list[str] = []


@register
class LogStage(Stage):
    """Like reproduce: a file under work/, recorded by absolute path,
    and a symlink into cache/ like the dev stage's source files."""

    name = "reloc_log"
    kind = Kind.DETERMINISTIC

    def run(self, ctx, unit_ids):
        out = []
        for uid in unit_ids:
            CALLS.append(self.name)
            log = ctx.workdir(uid) / "build.log"
            log.write_text(uid)
            src = ctx.paths.cache / "sources" / f"{uid}.asc"
            src.parent.mkdir(parents=True, exist_ok=True)
            src.write_text("sig")
            (ctx.workdir(uid) / "src.asc").symlink_to(src)
            out.append(StageResult(uid, Status.OK, {"log": str(log)}))
        return out


@register
class UseStage(Stage):
    """Like dev: reads the upstream file through its recorded path."""

    name = "reloc_use"
    kind = Kind.DETERMINISTIC

    def run(self, ctx, unit_ids):
        out = []
        for uid in unit_ids:
            CALLS.append(self.name)
            log = Path(ctx.result(uid, "reloc_log")["log"])
            out.append(StageResult(uid, Status.OK,
                                   {"read": log.read_text()}))
        return out


@register
class PackageStage(Stage):
    """A package stage after an item stage: its hash depends on which
    items the run selected."""

    name = "reloc_pkg"
    kind = Kind.DETERMINISTIC
    unit = UnitType.PACKAGE

    def run(self, ctx, unit_ids):
        CALLS.extend(self.name for _ in unit_ids)
        return [StageResult(u, Status.OK, {}) for u in unit_ids]


PIPELINE = """
[stage.reloc_log]
[stage.reloc_use]
after = ["reloc_log"]
[stage.reloc_pkg]
after = ["reloc_log"]
"""

FLT = Filter.from_dict({"arches": ["amd64"], "limit": 4})


@pytest.fixture
def moved(tmp_path, snapshot):
    """A project that ran once in old/, then was moved to new/."""
    old, new = tmp_path / "old", tmp_path / "new"
    old.mkdir()
    (old / "pipeline.toml").write_text(PIPELINE)
    app = App(old)
    store(app.db, snapshot, old / "state" / "s.json")
    app.run(FLT)
    # One unit asked to run again: it must still be due after the move.
    stale = app.db.one("SELECT unit_id FROM stage_result"
                       " WHERE stage='reloc_use'")["unit_id"]
    app.retry("reloc_use", [stale], "test")
    plan = app.plan(FLT)
    app.db.conn.close()
    shutil.move(old, new)
    CALLS.clear()
    return old, App(new), plan, stale


def logs(app):
    return [json.loads(r["data"])["log"] for r in app.db.query(
        "SELECT data FROM stage_result WHERE stage='reloc_log'")]


def test_relocate_keeps_plan_and_fixes_paths(moved):
    old, app, plan, stale = moved
    new = app.config.root

    rel = relocate(app, old)

    assert all(p.startswith(f"{new}/") and Path(p).exists()
               for p in logs(app))
    assert rel.fields["stage_result"] == 4
    assert rel.fields["snapshot"] == 1
    assert rel.hashes > 0  # without the remap, everything would re-run
    assert rel.links == 4
    for link in (new / "work").rglob("src.asc"):
        assert link.resolve().is_relative_to(new / "cache")
    assert app.plan(FLT) == plan
    app.run(FLT)
    # Only the retried unit. reloc_pkg stays cached because relocate
    # remaps the hashes of past runs' selections, not only the default.
    assert CALLS == ["reloc_use"]


def test_naive_rewrite_would_rerun(moved):
    old, app, plan, _ = moved
    app.db.execute("UPDATE stage_result SET data = replace(data, ?, ?)",
                   (f"{old}/", f"{app.config.root}/"))
    assert app.plan(FLT) != plan


def test_dry_run_changes_nothing(moved):
    old, app, plan, _ = moved
    before = logs(app)
    rel = relocate(app, old, dry_run=True)
    assert rel.hashes > 0 and rel.links == 4
    assert logs(app) == before
    assert app.plan(FLT) == plan
    links = list((app.config.root / "work").rglob("src.asc"))
    assert all(str(p.readlink()).startswith(f"{old}/") for p in links)


def test_refusals(moved, tmp_path, capsys):
    old, app, _, _ = moved
    with pytest.raises(ValueError, match="absolute"):
        relocate(app, Path("old"))
    with pytest.raises(ValueError, match="already"):
        relocate(app, app.config.root)
    with pytest.raises(ValueError, match="ASCII"):
        relocate(app, tmp_path / 'with"quote')
    app.db.execute("UPDATE run SET status='running', pid=NULL")
    with pytest.raises(ValueError, match="in progress"):
        relocate(app, old)


def test_cli(moved, capsys):
    old, app, _, _ = moved
    main(["--root", str(app.config.root), "relocate", str(old)])
    out = capsys.readouterr().out
    assert f"rewrote {old}/ -> {app.config.root}/" in out
    assert "symlinks under work/ and cache/: 4" in out
