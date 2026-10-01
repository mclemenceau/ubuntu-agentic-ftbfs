# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""reproduce stage offline: fake sbuild/pull-lp-source executables replay
a real Launchpad log; a fake PPA drives the async pending lifecycle."""

import json
import shutil
import stat
from pathlib import Path

import pytest

from ftbfs.builder import local, outcome
from ftbfs.builder.base import BuilderUnavailable
from ftbfs.builder.local import LocalBuilder, sbuild_command
from ftbfs.builder.pool import BuilderPool
from ftbfs.core.context import Paths, Units
from ftbfs.core.pipeline import build
from ftbfs.core.scheduler import Scheduler
from ftbfs.core.stage import discover
from ftbfs.filters import all_items
from ftbfs.inventory import store
from ftbfs.logs import extract, read_log
from ftbfs.rules import RuleSet
from ftbfs.stages import reproduce as repro_mod
from ftbfs.stages.reproduce import extra_repositories

ROOT = Path(__file__).parent.parent
LOG = Path(__file__).parent / "fixtures" / "logs" / "32891842.txt.gz"

FAKE_SBUILD = """#!/bin/sh
# last arg is the .dsc; write a .build log like sbuild does
dsc=$(eval echo \\${$#})
name=$(basename "$dsc" .dsc)
cp "$FAKE_SBUILD_LOG" "${name}_amd64.build"
touch "${name}_amd64.deb"
exit ${FAKE_SBUILD_EXIT:-2}
"""
FAKE_PULL = """#!/bin/sh
# pull-lp-source --download-only SRC VER
echo "Source: $2" > "$2_$3.dsc"
"""


@pytest.fixture
def env(db, snapshot, tmp_path, monkeypatch):
    store(db, snapshot, tmp_path / "s.json")
    item = dict(all_items(db, "WHERE i.arch='amd64' AND"
                          " i.state='FAILEDTOBUILD'")[0])
    bindir = tmp_path / "bin"
    bindir.mkdir()
    for name, body in (("sbuild", FAKE_SBUILD),
                       ("pull-lp-source", FAKE_PULL)):
        exe = bindir / name
        exe.write_text(body)
        exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{shutil.os.environ['PATH']}")
    plain = tmp_path / "lp.build"
    plain.write_text(read_log(LOG))
    monkeypatch.setenv("FAKE_SBUILD_LOG", str(plain))
    shutil.copy(ROOT / "rules.toml", tmp_path / "rules.toml")
    ex = extract(read_log(LOG)).to_dict()
    return db, item, tmp_path, ex


def run_repro(db, items, tmp_path, excerpts, options=None, run_id=None,
              builders=None):
    from ftbfs.core.stage import Kind, Stage, StageResult, Status, UnitType

    class Excerpt(Stage):
        name, kind, unit = "excerpt", Kind.DETERMINISTIC, UnitType.ITEM

        def run(self, ctx, ids):
            return [StageResult(i, Status.OK, excerpts[i]) for i in ids]

    registry = {**discover(), "excerpt": Excerpt}
    conf = {"excerpt": {},
            "reproduce": {"after": ["excerpt"], **(options or {})}}
    pipeline = build({"stage": conf}, registry, "fake")
    paths = Paths(tmp_path, tmp_path / "work", tmp_path / "cache")
    return Scheduler(db, pipeline, Units(items, db), {}, paths,
                     run_id=run_id, builders=builders).run()


def result(db, uid):
    row = db.one("SELECT status, data FROM stage_result WHERE unit_id=?"
                 " AND stage='reproduce' ORDER BY id DESC", (uid,))
    return row["status"], json.loads(row["data"])


def test_local_reproduced_same_signature(env):
    db, item, tmp, ex = env
    totals = run_repro(db, [item], tmp, {item["id"]: ex})
    assert totals["reproduce"] == {"ok": 1}
    status, data = result(db, item["id"])
    assert data["outcome"] == outcome.REPRODUCED
    assert data["where"] == "local" and data["signature"] == ex["signature"]
    assert data["builder"] == "local"
    start = db.one("SELECT payload FROM event WHERE type='build_start'")
    assert json.loads(start["payload"])["where"] == "local"
    build_dir = Path(data["log"]).parent
    assert not list(build_dir.glob("*.deb"))  # binaries cleaned up
    assert (build_dir / "command.txt").read_text().startswith("sbuild")


def test_local_builds_now(env, monkeypatch):
    db, item, tmp, ex = env
    monkeypatch.setenv("FAKE_SBUILD_EXIT", "0")
    run_repro(db, [item], tmp, {item["id"]: ex})
    assert result(db, item["id"])[1]["outcome"] == outcome.BUILT


def test_local_different_failure(env):
    db, item, tmp, ex = env
    other = dict(ex, signature="0000", key_lines=["something else"],
                 signature_text="x y z")
    run_repro(db, [item], tmp, {item["id"]: other})
    assert result(db, item["id"])[1]["outcome"] == outcome.DIFFERENT


def test_local_same_class_other_signature_is_similar(env):
    db, item, tmp, ex = env
    # same fortify class, different normalized text
    similar = dict(ex, signature="1111")
    run_repro(db, [item], tmp, {item["id"]: similar})
    assert result(db, item["id"])[1]["outcome"] == outcome.SIMILAR


def test_foreign_arch_without_ppa_is_not_eligible(env):
    db, item, tmp, ex = env
    item = dict(item, arch="s390x", id=item["id"].replace("amd64",
                                                           "s390x"))
    totals = run_repro(db, [item], tmp, {item["id"]: ex})
    assert totals == {"excerpt": {"ok": 1}}


class DownBuilder(LocalBuilder):
    def build(self, *a, **kw):
        raise BuilderUnavailable("host unreachable")


def test_build_moves_to_another_builder_when_one_is_down(env):
    db, item, tmp, ex = env
    pool = BuilderPool([DownBuilder("down", 4), LocalBuilder("up", 1)])
    totals = run_repro(db, [item], tmp, {item["id"]: ex}, builders=pool)
    assert totals["reproduce"] == {"ok": 1}
    status, data = result(db, item["id"])
    assert data["builder"] == "up" and data["outcome"] == outcome.REPRODUCED
    down = db.one("SELECT payload FROM event WHERE type='builder_down'")
    assert json.loads(down["payload"])["builder"] == "down"
    assert pool.down() == {"down": "host unreachable"}


def test_all_builders_down_waits_for_the_next_run(env):
    db, item, tmp, ex = env
    pool = BuilderPool([DownBuilder("down", 1)])
    totals = run_repro(db, [item], tmp, {item["id"]: ex}, run_id=1,
                       builders=pool)
    # pending, not error: a host outage must not use up the error cap
    assert totals["reproduce"] == {"pending": 1}
    status, data = result(db, item["id"])
    assert data["phase"] == "waiting-for-builder"
    assert "every builder for amd64 is down" in data["reason"]
    # the next run, with the host back, builds it
    totals = run_repro(db, [item], tmp, {item["id"]: ex}, run_id=2)
    assert totals["reproduce"] == {"ok": 1}


def test_arch_no_builder_supports_is_not_eligible(env):
    db, item, tmp, ex = env
    item = dict(item, arch="i386", id=item["id"].replace("amd64", "i386"))
    totals = run_repro(db, [item], tmp, {item["id"]: ex},
                       {"local_arches": ["amd64", "i386"]})
    assert totals == {"excerpt": {"ok": 1}}


class FakePPA:
    """Scripted Launchpad: None (not published) -> building -> failed."""

    states: list = []
    copies: list = []

    def __init__(self, credentials, ref, series):
        self.ref = ref

    def processors(self):
        return {"amd64", "s390x"}

    def copy(self, source, version):
        FakePPA.copies.append((source, version))

    def status(self, source, version, arch):
        state = FakePPA.states.pop(0)
        if state is None:
            return None
        from ftbfs.builder.ppa import PPABuild
        return PPABuild(state, arch, "https://lp/build/1",
                        "https://lp/log.gz")


def test_ppa_pending_lifecycle(env, monkeypatch):
    db, item, tmp, ex = env
    item = dict(item, arch="s390x", id=item["id"].replace("amd64",
                                                           "s390x"))
    monkeypatch.setattr(repro_mod, "PPA", FakePPA)
    monkeypatch.setattr(repro_mod, "fetch_build_log",
                        lambda url, dest: shutil.copy(
                            shutil.os.environ["FAKE_SBUILD_LOG"], dest)
                        and Path(dest))
    FakePPA.states = [None, "Currently building", "Failed to build"]
    FakePPA.copies = []
    opts = {"ppa": "someone/repro"}

    def new_run():
        return db.execute(
            "INSERT INTO run (started, status, filter, pipeline_hash,"
            " trigger) VALUES ('t','running','{}','h','test')").lastrowid

    # run 1: copy requested -> pending, polled once (no busy loop)
    run_repro(db, [item], tmp, {item["id"]: ex}, opts, new_run())
    assert FakePPA.copies == [(item["source"], item["version"])]
    assert result(db, item["id"])[0] == "pending"
    assert len(FakePPA.states) == 2
    # run 2: still building -> pending, updated in place
    run_repro(db, [item], tmp, {item["id"]: ex}, opts, new_run())
    status, data = result(db, item["id"])
    assert status == "pending" and data["phase"] == "building"
    rows = db.query("SELECT 1 FROM stage_result WHERE stage='reproduce'")
    assert len(rows) == 1
    # run 3: failed -> log fetched and judged
    run_repro(db, [item], tmp, {item["id"]: ex}, opts, new_run())
    status, data = result(db, item["id"])
    assert status == "ok" and data["outcome"] == outcome.REPRODUCED
    assert data["where"] == "ppa" and FakePPA.copies == [
        (item["source"], item["version"])]


def test_helpers():
    assert extra_repositories("universe", "s") == []
    repos = extra_repositories("multiverse", "stonking")
    assert len(repos) == 2 and all("multiverse" in r for r in repos)
    cmd = sbuild_command(Path("/x/p_1.dsc"), "amd64", "stonking-proposed",
                         repos)
    assert "--dist=stonking-proposed" in cmd and cmd[-1] == "/x/p_1.dsc"
    assert sum(c.startswith("--extra-repository") for c in cmd) == 2


def test_judge_infra_when_no_log():
    rules = RuleSet.load(ROOT / "rules.toml")
    verdict, _ = outcome.judge(False, None, {}, rules, "p")
    assert verdict == outcome.INFRA


QUIET_SBUILD = """#!/bin/sh
dsc=$(eval echo \\${$#})
log="$(basename "$dsc" .dsc)_amd64.build"
i=0
while [ $i -lt "$CHATTY_TICKS" ]; do  # output keeps growing
  echo "compiling $i" >> "$log"; i=$((i + 1)); sleep 0.2
done
[ -n "$HANG" ] && sleep 30  # a hung test suite: no more output
exit 0
"""


@pytest.mark.parametrize("hang", [True, False])
def test_build_killed_when_its_log_stops_growing(tmp_path, monkeypatch,
                                                 hang):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "sbuild"
    exe.write_text(QUIET_SBUILD)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{shutil.os.environ['PATH']}")
    monkeypatch.setenv("CHATTY_TICKS", "8")  # 1.6 s of steady output
    monkeypatch.setenv("HANG", "1" if hang else "")
    monkeypatch.setattr(local, "POLL_S", 0.1)
    b = local.build(tmp_path / "p_1.dsc", "amd64", "d", tmp_path / "b",
                    timeout=60, idle_timeout=1)
    assert b.stalled is hang and b.timed_out is hang and b.ok is not hang
    assert b.duration_s < 5  # not the 30 s hang, nor the 60 s timeout
