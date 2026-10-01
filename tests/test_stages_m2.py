# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""excerpt + classify stages end to end, offline (pre-seeded log cache)."""

import json
import shutil
from pathlib import Path

from ftbfs.core.context import Paths, Units
from ftbfs.core.pipeline import build
from ftbfs.core.scheduler import Scheduler
from ftbfs.core.stage import discover
from ftbfs.filters import all_items
from ftbfs.inventory import store

ROOT = Path(__file__).parent.parent
LOG = Path(__file__).parent / "fixtures" / "logs" / "32891842.txt.gz"


def test_excerpt_and_classify(db, snapshot, tmp_path):
    store(db, snapshot, tmp_path / "s.json")
    item = dict(all_items(db, "WHERE i.state='FAILEDTOBUILD'")[0])
    cache = tmp_path / "cache"
    (cache / "logs").mkdir(parents=True)
    shutil.copy(LOG, cache / "logs" / f"{item['build_id']}.txt.gz")

    registry = discover()
    pipeline = build({"stage": {"excerpt": {},
                                "classify": {"after": ["excerpt"]}}},
                     registry, "fake")
    shutil.copy(ROOT / "rules.toml", tmp_path / "rules.toml")
    paths = Paths(tmp_path, tmp_path / "work", cache)
    units = Units([item])
    totals = Scheduler(db, pipeline, units, {}, paths).run()
    assert totals == {"excerpt": {"ok": 1}, "classify": {"ok": 1}}

    row = db.one("SELECT cluster_id, class, family FROM item WHERE id=?",
                 (item["id"],))
    assert tuple(row) == ("fortify-source-redefined",
                          "fortify-source-redefined", "toolchain")
    excerpt = (tmp_path / "work" / item["source"] / item["version"]
               / item["arch"] / "excerpt" / "excerpt.txt")
    assert "_FORTIFY_SOURCE" in excerpt.read_text()

    # Editing rules.toml re-classifies, without re-extracting.
    rules = (tmp_path / "rules.toml").read_text()
    (tmp_path / "rules.toml").write_text(
        rules.replace('id = "fortify-redefined"\nclass = '
                      '"fortify-source-redefined"',
                      'id = "fortify-redefined"\nclass = "fortify"'))
    totals = Scheduler(db, pipeline, Units([item]), {}, paths).run()
    assert totals == {"classify": {"ok": 1}}
    data = json.loads(db.one(
        "SELECT data FROM stage_result WHERE stage='classify'"
        " ORDER BY id DESC")["data"])
    assert data["class"] == "fortify"
