# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Volatile upstream fields and `ftbfs rehash`: a re-run that finds the
same thing keeps the stages after it cached and their approvals bound,
and moving to that hash recipe changes no decision."""

import itertools

import pytest

from ftbfs.app import App
from ftbfs.cli import main
from ftbfs.core import scheduler
from ftbfs.core.stage import Kind, Stage, StageResult, Status, register
from ftbfs.filters import Filter
from ftbfs.inventory import store
from ftbfs.rehash import RESTORED, rehash

CALLS: list[str] = []
SIGNATURE: dict[str, str] = {}
FIX_STATUS: dict[str, Status] = {}
RUNS = itertools.count(1)


@register
class BuildStage(Stage):
    """Like reproduce: each build takes its own time and leaves its log
    in a new attempt dir; the signature is what it found."""

    name = "rh_build"
    kind = Kind.DETERMINISTIC
    volatile = ("duration_s", "log")

    def run(self, ctx, unit_ids):
        n = next(RUNS)
        return [StageResult(u, Status.OK, {
            "outcome": "reproduced", "signature": SIGNATURE.get(u, "s1"),
            "duration_s": n, "log": f"/work/{u}/attempt-{n}/x.build"})
            for u in unit_ids]


@register
class FixStage(Stage):
    """Like dev: gated and paid."""

    name = "rh_fix"
    kind = Kind.DETERMINISTIC

    def run(self, ctx, unit_ids):
        CALLS.extend(unit_ids)
        return [StageResult(u, FIX_STATUS.get(u, Status.OK),
                            {"fix": ctx.result(u, "rh_build")["signature"]})
                for u in unit_ids]


@register
class CheckStage(Stage):
    """Like verify."""

    name = "rh_check"
    kind = Kind.DETERMINISTIC

    def run(self, ctx, unit_ids):
        return [StageResult(u, Status.OK, {}) for u in unit_ids]


PIPELINE = """
[stage.rh_build]
[stage.rh_fix]
after = ["rh_build"]
gate = "manual"
max_errors = 1
[stage.rh_check]
after = ["rh_fix"]
"""

FLT = Filter.from_dict({"arches": ["amd64"], "limit": 4})


@pytest.fixture
def app(tmp_path, snapshot):
    CALLS.clear()
    SIGNATURE.clear()
    FIX_STATUS.clear()
    (tmp_path / "pipeline.toml").write_text(PIPELINE)
    app = App(tmp_path)
    store(app.db, snapshot, tmp_path / "state" / "s.json")
    return app


def approve_and_fix(app):
    """Run, approve every fix, run: each unit fixed, approval bound."""
    app.run(FLT)
    uids = [r["id"] for r in app.workable(FLT)]
    app.approve("rh_fix", uids, "approved", "test")
    app.run(FLT)
    assert sorted(CALLS) == sorted(uids)
    CALLS.clear()
    return uids


def gate(app, uid):
    return app.db.one("SELECT * FROM gate WHERE unit_id=? AND"
                      " stage='rh_fix'", (uid,))


def rebuild(app, uid):
    app.retry("rh_build", [uid], "test")
    app.run(FLT)


def test_same_finding_keeps_the_fix_cached_and_approved(app):
    uid = approve_and_fix(app)[0]
    bound = gate(app, uid)["inputs"]
    rebuild(app, uid)  # new duration and log, same signature
    assert CALLS == []
    assert gate(app, uid)["decision"] == "approved"
    assert gate(app, uid)["inputs"] == bound
    assert dict(app.explain([uid], FLT)[uid]["stages"])["rh_fix"] \
        == "cached (ok)"


def test_a_new_signature_reopens_the_gate(app):
    uid = approve_and_fix(app)[0]
    SIGNATURE[uid] = "s2"
    rebuild(app, uid)
    assert CALLS == []
    assert gate(app, uid)["decision"] == "pending"
    assert app.db.one("SELECT 1 FROM event WHERE type='gate_reopened'"
                      " AND unit=?", (uid,))


@pytest.fixture
def recipe1(app, monkeypatch):
    """A database from before recipe 2, as production has it:
    - noise: rebuilt with the same signature, gate reopened anyway;
    - kept: untouched, its approval still bound and current;
    - changed: rebuilt with a new signature, gate reopened;
    - failed: its last fix errored, then rebuilt, gate reopened."""
    monkeypatch.setattr(scheduler, "HASH_RECIPE", 1)
    noise, kept, changed, failed = approve_and_fix(app)
    FIX_STATUS[failed] = Status.ERROR
    app.retry("rh_fix", [failed], "test")
    app.run(FLT)
    CALLS.clear()
    SIGNATURE[changed] = "s2"
    for uid in (noise, changed, failed):
        app.retry("rh_build", [uid], "test")
    app.run(FLT)
    assert CALLS == []
    assert [gate(app, u)["decision"] for u in (noise, kept, changed,
                                               failed)] \
        == ["pending", "approved", "pending", "pending"]
    plan = app.plan(FLT)
    monkeypatch.setattr(scheduler, "HASH_RECIPE", 2)
    return app, plan, (noise, kept, changed, failed)


def state(app):
    return (app.db.query("SELECT * FROM gate ORDER BY unit_id, stage"),
            app.db.query("SELECT id, inputs_hash FROM stage_result"
                         " ORDER BY id"))


def test_without_rehash_the_new_recipe_reopens_approvals(recipe1):
    app, plan, (noise, kept, changed, failed) = recipe1
    assert kept in plan["rh_fix"]["done"]
    assert kept in app.plan(FLT)["rh_fix"]["gate"]


def test_rehash_changes_no_decision_but_the_restored(recipe1):
    app, plan, (noise, kept, changed, failed) = recipe1
    before = state(app)
    dry = rehash(app, dry_run=True)
    assert state(app) == before
    r = rehash(app)
    assert (dry.results, dry.bindings, dry.restored) == \
        (r.results, r.bindings, r.restored)
    assert f"rh_fix {kept}" in r.results and r.bindings == [
        f"rh_fix {kept}"]
    assert r.restored == [f"rh_fix {noise}"]
    assert r.kept == {"its inputs changed": [changed],
                      "its latest result is not ok": [failed]}

    plan["rh_fix"]["gate"].remove(noise)
    plan["rh_fix"]["done"].append(noise)
    after = app.plan(FLT)
    assert {s: {k: sorted(v) for k, v in per.items()}
            for s, per in after.items()} == \
        {s: {k: sorted(v) for k, v in per.items()}
         for s, per in plan.items()}
    app.run(FLT)
    assert CALLS == []  # nothing paid
    g = gate(app, noise)
    assert (g["decision"], g["by"], g["note"]) == ("approved",
                                                   "migration", RESTORED)
    assert app.db.one("SELECT 1 FROM event WHERE type='gate_restored'"
                      " AND unit=?", (noise,))


def test_restore_only_on_an_exact_match(recipe1):
    app, _, (noise, kept, changed, failed) = recipe1
    # A retry of the fix since: its stored hash is for another nonce.
    app.retry("rh_fix", [noise], "test")
    r = rehash(app)
    assert r.restored == []
    assert noise in r.kept["its inputs changed"]
    assert gate(app, noise)["decision"] == "pending"


def test_rehash_is_idempotent(recipe1, capsys):
    app = recipe1[0]
    rehash(app)
    before = state(app)
    r = rehash(app)
    assert (r.results, r.bindings, r.restored) == ([], [], [])
    assert state(app) == before
    main(["--root", str(app.config.root), "rehash", "--dry-run"])
    out = capsys.readouterr().out
    assert "results moved onto the new hash: 0" in out
    assert "approvals restored: 0" in out


def test_refused_during_a_run(app):
    app.run(FLT)
    app.db.execute("UPDATE run SET status='running', pid=NULL")
    with pytest.raises(ValueError, match="in progress"):
        rehash(app)
