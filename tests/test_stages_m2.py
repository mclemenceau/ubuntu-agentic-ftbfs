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
    (cache / "logs" / f"{item['build_id']}.finished").write_text(
        item["finished_at"])

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


RETRY_LOG = LOG.parent / "33505747.txt.gz"


def test_launchpad_retry_is_extracted_again(db, snapshot, tmp_path,
                                            monkeypatch):
    """Launchpad retries a failed build under the same build id and log
    URL; only the finish time says there is a new log. Its failure, not
    the first attempt's, is what the item must show."""
    store(db, snapshot, tmp_path / "s.json")
    item = dict(all_items(db, "WHERE i.state='FAILEDTOBUILD'")[0])
    served = {item["log_url"]: LOG}

    class Response:
        def __init__(self, path):
            self.data = path.read_bytes()

        def __enter__(self):
            return self

        def __exit__(self, *exc):
            return False

        def read(self):
            return self.data

    monkeypatch.setattr("ftbfs.logs.urllib.request.urlopen",
                        lambda req, timeout: Response(
                            served[req.full_url]))

    registry = discover()
    pipeline = build({"stage": {"excerpt": {},
                                "classify": {"after": ["excerpt"]}}},
                     registry, "fake")
    shutil.copy(ROOT / "rules.toml", tmp_path / "rules.toml")
    paths = Paths(tmp_path, tmp_path / "work", tmp_path / "cache")

    def run():
        return Scheduler(db, pipeline, Units([item]), {}, paths).run()

    def cls():
        return db.one("SELECT class FROM item WHERE id=?",
                      (item["id"],))["class"]

    assert run() == {"excerpt": {"ok": 1}, "classify": {"ok": 1}}
    assert cls() == "fortify-source-redefined"

    # The same snapshot again: nothing to redo.
    assert run() == {}

    # The retry fails differently, at the same URL, later.
    served[item["log_url"]] = RETRY_LOG
    item["finished_at"] = "2026-11-21 11:01:36"
    db.execute("UPDATE item SET finished_at=? WHERE id=?",
               (item["finished_at"], item["id"]))
    assert run() == {"excerpt": {"ok": 1}, "classify": {"ok": 1}}
    assert cls() == "dependency-unsatisfiable"
    data = json.loads(db.one(
        "SELECT data FROM stage_result WHERE stage='excerpt'"
        " ORDER BY id DESC")["data"])
    assert data["fail_stage"] == "install-deps"
    # The log keeps its path: an excerpt that did not change stays the
    # same result, and nothing after it runs again.
    assert data["log_path"] == str(
        tmp_path / "cache" / "logs" / f"{item['build_id']}.txt.gz")
