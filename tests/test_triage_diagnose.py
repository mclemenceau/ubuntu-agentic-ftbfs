"""triage + diagnose stages with the fake backend over seeded results."""

import json
from pathlib import Path

import pytest

from ftbfs.agents.fake import FakeBackend
from ftbfs.core.context import Paths, Units
from ftbfs.core.pipeline import build
from ftbfs.core.scheduler import Scheduler
from ftbfs.core.stage import (
    Kind,
    Stage,
    StageResult,
    Status,
    UnitType,
    discover,
)
from ftbfs.filters import all_items
from ftbfs.inventory import store

ROOT = Path(__file__).parent.parent


class Seed(Stage):
    """Stands in for excerpt/classify/facts: returns canned data."""

    kind = Kind.DETERMINISTIC
    data: dict = {}

    def run(self, ctx, unit_ids):
        return [StageResult(u, Status.OK, self.data.get(u, {}))
                for u in unit_ids]


def seed_stage(name, unit, data):
    return type(f"Seed_{name}", (Seed,), {
        "name": name, "unit": unit, "data": data})


@pytest.fixture
def setup(db, snapshot, tmp_path):
    store(db, snapshot, tmp_path / "s.json")
    rows = [dict(r) for r in all_items(
        db, "WHERE i.state='FAILEDTOBUILD' AND i.arch='amd64'")[:3]]
    # two clusters: a dependency one and an unknown compile one
    rows[0]["cluster_id"] = "dependency-unsatisfiable:libfoo-dev"
    rows[1]["cluster_id"] = rows[2]["cluster_id"] = "sig:abc"
    for r in rows:
        db.execute("UPDATE item SET cluster_id=? WHERE id=?",
                   (r["cluster_id"], r["id"]))
    (tmp_path / "prompts").mkdir()
    for p in ("triage", "diagnose"):
        (tmp_path / "prompts" / f"{p}.md").write_text(
            (ROOT / "prompts" / f"{p}.md").read_text())
    excerpt = {r["id"]: {"key_lines": [f"error: boom in {r['source']}"],
                         "text": "some excerpt", "step": "dh_auto_build"}
               for r in rows}
    classify = {r["id"]: {"class": "dependency-unsatisfiable"
                          if r is rows[0] else "unknown",
                          "family": "deps" if r is rows[0] else None,
                          "hint": ""} for r in rows}
    return db, rows, excerpt, classify, tmp_path


def run(db, rows, tmp_path, excerpt, classify, facts, responder):
    registry = {**discover(),
                "excerpt": seed_stage("excerpt", UnitType.ITEM, excerpt),
                "classify": seed_stage("classify", UnitType.ITEM, classify),
                "facts": seed_stage("facts", UnitType.PACKAGE, facts)}
    conf = {"excerpt": {}, "classify": {"after": ["excerpt"]},
            "facts": {},
            "triage": {"after": ["classify", "facts"]},
            "diagnose": {"after": ["triage"], "when":
                         "triage.fixable != 'no' and triage.action in "
                         "['patch', 'investigate']"}}
    backend = FakeBackend(responder=responder)
    pipeline = build({"stage": conf}, registry, "fake")
    paths = Paths(tmp_path, tmp_path / "work", tmp_path / "cache")
    totals = Scheduler(db, pipeline, Units(rows), {"fake": backend},
                       paths).run()
    return totals, backend


def facts_for(rows, signals):
    return {r["source"]: {
        "signals": signals, "ubuntu": {"newest_failing": r["version"],
                                       "delta": False},
        "debian": {"unstable": "9.9-1", "experimental": None,
                   "testing_repro": {"arches": {}}},
        "debian_bugs": {"open": [], "fixed_newer": []},
        "upstream": {"repo": None, "homepage": None}} for r in rows}


def llm_verdicts(req):
    if "fix_kind" in req.output_schema["properties"]:  # diagnose
        return {"root_cause": "The header declares foo() without a"
                " prototype, which C23 rejects.",
                "evidence": ["error: boom"],
                "fix_kind": "code-patch",
                "fix_strategy": "Add a real prototype for foo() in a.h.",
                "patch_outline": ["edit a.h to add the prototype"],
                "upstream": "not reported",
                "applies_to_all_members": True, "risk": "low",
                "confidence": 0.7, "needs_source": True}
    packs = json.JSONDecoder().raw_decode(req.prompt)[0]
    return {"clusters": [{
        "id": p["id"], "category": "compile", "summary": "boom",
        "root_cause_guess": None, "obvious": False, "fixable": "yes",
        "action": "patch", "confidence": 0.6} for p in packs]}


def test_dependency_cluster_decided_without_llm(setup):
    db, rows, excerpt, classify, tmp = setup
    totals, backend = run(db, rows, tmp, excerpt, classify,
                          facts_for(rows, []), llm_verdicts)
    dep = json.loads(db.one(
        "SELECT data FROM stage_result WHERE stage='triage' AND unit_id=?",
        (rows[0]["cluster_id"],))["data"])
    assert dep["decided_by"] == "rules"
    assert dep["action"] == "wait-dependency"
    assert "libfoo-dev" in dep["summary"]
    # one packed triage call for the unknown cluster + one diagnosis
    assert totals["triage"] == {"ok": 2}
    assert totals["diagnose"] == {"ok": 1}
    assert len(backend.calls) == 2
    triage_prompt = json.JSONDecoder().raw_decode(
        backend.calls[0].prompt)[0]
    assert [p["id"] for p in triage_prompt] == ["sig:abc"]
    assert triage_prompt[0]["items"] == 2
    assert backend.calls[0].tier == "small"
    assert backend.calls[1].tier == "small"  # default tier in this conf


def test_fixed_in_debian_cluster_needs_no_llm(setup):
    db, rows, excerpt, classify, tmp = setup
    facts = facts_for(rows, ["sync-candidate", "fixed-in-debian"])
    totals, backend = run(db, rows, tmp, excerpt, classify, facts,
                          llm_verdicts)
    assert backend.calls == []
    verdicts = {r["unit_id"]: json.loads(r["data"]) for r in db.query(
        "SELECT unit_id, data FROM stage_result WHERE stage='triage'")}
    assert verdicts["sig:abc"]["action"] == "sync"
    assert "diagnose" not in totals  # sync is not a diagnose action


def test_missing_verdict_is_an_error_not_a_guess(setup):
    db, rows, excerpt, classify, tmp = setup
    totals, _ = run(db, rows, tmp, excerpt, classify, facts_for(rows, []),
                    lambda req: {"clusters": []})
    assert totals["triage"]["error"] >= 1


def test_placeholder_diagnosis_is_rejected(setup):
    db, rows, excerpt, classify, tmp = setup

    def lazy(req):
        if "fix_kind" in req.output_schema["properties"]:
            return {"root_cause": "test", "evidence": ["a"],
                    "fix_kind": "code-patch", "fix_strategy": "test",
                    "patch_outline": ["a"], "upstream": "test",
                    "applies_to_all_members": True, "risk": "low",
                    "confidence": 0.5, "needs_source": True}
        return llm_verdicts(req)

    totals, _ = run(db, rows, tmp, excerpt, classify, facts_for(rows, []),
                    lazy)
    assert "ok" not in totals["diagnose"]
    row = db.one("SELECT data FROM stage_result WHERE stage='diagnose'")
    assert "shorter than" in json.loads(row["data"])["error"]
