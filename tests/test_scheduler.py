"""Scheduler behaviour, exercised with throwaway stages and the fake
backend: zero tokens, no network."""

import json

import pytest

from ftbfs.agents.fake import FakeBackend
from ftbfs.core.context import Paths, Units
from ftbfs.core.pipeline import PipelineError, build
from ftbfs.core.scheduler import Cancelled, Scheduler
from ftbfs.core.stage import Kind, Stage, StageResult, Status, UnitType
from ftbfs.filters import Filter, select
from ftbfs.inventory import store

CALLS: list[tuple[str, str]] = []


def make_stage(name, kind=Kind.DETERMINISTIC, unit=UnitType.ITEM,
               version="1", fn=None, batch_size=1):
    """fn(ctx, uid) -> StageResult | dict (ok with that data)."""

    class S(Stage):
        def run(self, ctx, unit_ids):
            out = []
            for uid in unit_ids:
                CALLS.append((name, uid))
                res = fn(ctx, uid) if fn else {"v": version}
                if not isinstance(res, StageResult):
                    res = StageResult(uid, Status.OK, res)
                out.append(res)
            return out

    S.name, S.kind, S.unit, S.version = name, kind, unit, version
    S.batch_size = batch_size
    S.__name__ = f"Stage_{name}"
    return S


@pytest.fixture
def units(db, snapshot, tmp_path):
    store(db, snapshot, tmp_path / "s.json")
    CALLS.clear()
    flt = Filter.from_dict({"arches": ["amd64"], "limit": 5})
    return Units(select(db, flt))


def sched(db, units, tmp_path, stages, conf, backend=None, run_id=None):
    registry = {s.name: s for s in stages}
    pipeline = build({"stage": conf}, registry, default_backend="fake")
    backend = backend or FakeBackend()
    paths = Paths(tmp_path, tmp_path / "work", tmp_path / "cache")
    return Scheduler(db, pipeline, units, {"fake": backend}, paths,
                     run_id=run_id)


def calls(stage):
    return [u for s, u in CALLS if s == stage]


def test_dag_order_and_cache(db, units, tmp_path):
    a, b = make_stage("a"), make_stage("b")
    pkg = make_stage("pkg", unit=UnitType.PACKAGE)
    conf = {"b": {"after": ["a", "pkg"]}, "a": {}, "pkg": {"after": ["a"]}}
    s = sched(db, units, tmp_path, [a, b, pkg], conf)
    assert list(s.pipeline.specs) == ["a", "pkg", "b"]

    totals = s.run()
    assert totals["a"] == {"ok": 5}
    assert totals["pkg"] == {"ok": len(units.of(UnitType.PACKAGE))}
    assert totals["b"] == {"ok": 5}
    order = [st for st, _ in CALLS]
    assert order.index("pkg") > max(i for i, x in enumerate(order)
                                    if x == "a")

    CALLS.clear()
    assert s.run() == {}  # everything cached
    assert CALLS == []


def test_version_bump_reruns_stage_and_downstream_only(db, units,
                                                        tmp_path):
    conf = {"a": {}, "b": {"after": ["a"]}, "c": {"after": ["b"]},
            "d": {}}
    v1 = [make_stage(n) for n in "abcd"]
    sched(db, units, tmp_path, v1, conf).run()

    CALLS.clear()
    v2 = [make_stage("a"), make_stage("b", version="2"), make_stage("c"),
          make_stage("d")]
    sched(db, units, tmp_path, v2, conf).run()
    assert calls("a") == [] and calls("d") == []
    assert len(calls("b")) == 5 and len(calls("c")) == 5


def test_when_predicate_and_explain(db, units, tmp_path):
    first = next(iter(units.items))

    def triage(ctx, uid):
        return {"fixable": "yes" if uid == first else "no"}

    stages = [make_stage("triage", fn=triage), make_stage("diagnose")]
    conf = {"triage": {},
            "diagnose": {"after": ["triage"],
                         "when": "triage.fixable != 'no'"}}
    s = sched(db, units, tmp_path, stages, conf)
    s.run()
    assert calls("diagnose") == [first]

    other = [u for u in units.items if u != first][0]
    reasons = dict(s.explain({"item": other}))
    assert reasons["diagnose"] == "when is false: triage.fixable != 'no'"
    assert dict(s.explain({"item": first}))["diagnose"] == "cached (ok)"


def test_manual_gate(db, units, tmp_path):
    stages = [make_stage("a"), make_stage("dev")]
    conf = {"a": {}, "dev": {"after": ["a"], "gate": "manual"}}
    s = sched(db, units, tmp_path, stages, conf)
    s.run()
    assert calls("dev") == []
    pending = db.query("SELECT unit_id FROM gate WHERE decision='pending'")
    assert len(pending) == 5
    assert len(db.query("SELECT 1 FROM event WHERE type='gate_wait'")) == 5

    s.run()  # no duplicate gate rows or events
    assert len(db.query("SELECT 1 FROM event WHERE type='gate_wait'")) == 5

    chosen = pending[0]["unit_id"]
    db.execute("UPDATE gate SET decision='approved' WHERE unit_id=?",
               (chosen,))
    s.run()
    assert calls("dev") == [chosen]


def test_plan_is_a_dry_run_and_gates_queue_beyond_until(db, units,
                                                        tmp_path):
    stages = [make_stage("a"), make_stage("dev"), make_stage("b")]
    conf = {"a": {}, "dev": {"after": ["a"], "gate": "manual"},
            "b": {"after": ["dev"]}}
    s = sched(db, units, tmp_path, stages, conf)
    plan = s.plan()
    assert len(plan["a"]["ready"]) == 5
    assert len(plan["dev"]["blocked"]) == 5 and "ready" not in plan["dev"]
    assert CALLS == [] and not db.query("SELECT 1 FROM gate")

    s.run(until="a")  # dev is past `until` but still queues its gates
    assert calls("dev") == []
    assert len(db.query("SELECT 1 FROM gate WHERE decision='pending'")) == 5
    plan = s.plan(["a", "dev"])
    assert set(plan) == {"a", "dev"}
    assert len(plan["a"]["done"]) == 5 and len(plan["dev"]["gate"]) == 5


def test_outward_stage_gate_is_forced():
    out = make_stage("file_bug", kind=Kind.OUTWARD)
    p = build({"stage": {"file_bug": {}}}, {"file_bug": out}, "fake")
    assert p["file_bug"].gate == "manual"
    assert p["file_bug"].notes


def test_on_fail_loop_is_bounded(db, units, tmp_path):
    def review(ctx, uid):
        return StageResult(uid, Status.FAIL, {"findings": ["nope"]})

    def dev(ctx, uid):
        return {"feedback": [f["data"]["findings"] for f in
                             ctx.feedback(uid)]}

    stages = [make_stage("dev", fn=dev), make_stage("review", fn=review)]
    conf = {"dev": {},
            "review": {"after": ["dev"],
                       "on_fail": {"goto": "dev", "max_loops": 2}}}
    s = sched(db, units, tmp_path, stages, conf)
    s.run()
    uid = next(iter(units.items))
    assert calls("dev").count(uid) == 3  # initial + 2 loops
    assert calls("review").count(uid) == 3
    exhausted = db.query("SELECT 1 FROM event WHERE type='loop_exhausted'"
                         " AND unit=?", (uid,))
    assert len(exhausted) == 1
    last_dev = json.loads(db.one(
        "SELECT data FROM stage_result WHERE unit_id=? AND stage='dev'"
        " ORDER BY id DESC", (uid,))["data"])
    assert last_dev["feedback"] == [["nope"], ["nope"]]


def test_loop_stops_when_review_passes(db, units, tmp_path):
    def review(ctx, uid):
        dev = ctx.upstream(uid, "dev")
        status = Status.OK if dev["round"] >= 1 else Status.FAIL
        return StageResult(uid, status, {})

    def dev(ctx, uid):
        return {"round": len(ctx.feedback(uid))}

    stages = [make_stage("dev", fn=dev), make_stage("review", fn=review)]
    conf = {"dev": {},
            "review": {"after": ["dev"],
                       "on_fail": {"goto": "dev", "max_loops": 5}}}
    sched(db, units, tmp_path, stages, conf).run()
    uid = next(iter(units.items))
    assert calls("dev").count(uid) == 2


def test_errors_are_retried_then_capped(db, units, tmp_path):
    def boom(ctx, uid):
        raise RuntimeError("network down")

    s = sched(db, units, tmp_path, [make_stage("flaky", fn=boom)],
              {"flaky": {}})
    s.run()
    uid = next(iter(units.items))
    assert calls("flaky").count(uid) == 3
    row = db.one("SELECT data FROM stage_result WHERE unit_id=?", (uid,))
    assert "network down" in json.loads(row["data"])["error"]
    assert dict(s.explain({"item": uid}))["flaky"].startswith("gave up")


def test_retry_request_forces_rerun(db, units, tmp_path):
    s = sched(db, units, tmp_path, [make_stage("a")], {"a": {}})
    s.run()
    uid = next(iter(units.items))
    db.event("retry", unit=uid, stage="a")
    CALLS.clear()
    s.run()
    assert calls("a") == [uid]


def test_cancel(db, units, tmp_path):
    run_id = db.execute(
        "INSERT INTO run (started, status, control, filter, pipeline_hash,"
        " trigger) VALUES ('t', 'running', 'cancel', '{}', 'h', 'test')"
    ).lastrowid
    s = sched(db, units, tmp_path, [make_stage("a")], {"a": {}},
              run_id=run_id)
    with pytest.raises(Cancelled):
        s.run()


SCHEMA = {
    "type": "object",
    "required": ["items"],
    "properties": {"items": {"type": "array", "items": {
        "type": "object", "required": ["id", "fixable"],
        "properties": {"fixable": {"enum": ["yes", "no"]}}}}},
}


def test_agent_batch_ledger_repair_and_artifacts(db, units, tmp_path):
    seen_prompts = []

    def responder(req):
        seen_prompts.append(req.prompt)
        if req.prompt.startswith("Your previous answer"):
            ids = json.loads(req.prompt.split("Previous answer:\n")[1]
                             .split("\n\nWhen done")[0])["ids"]
            return {"items": [{"id": i, "fixable": "yes"} for i in ids]}
        ids = [line[2:] for line in req.prompt.splitlines()
               if line.startswith("- ")]
        if len(ids) == 3:  # first batch answers badly -> repair
            return {"ids": ids}
        return {"items": [{"id": i, "fixable": "no"} for i in ids]}

    def run(self, ctx, unit_ids):
        prompt = "Triage:\n" + "\n".join(f"- {u}" for u in unit_ids)
        res = ctx.run_agent(unit_ids, prompt, output_schema=SCHEMA)
        by_id = {i["id"]: i for i in res.data["items"]}
        return [StageResult(u, Status.OK, by_id[u]) for u in unit_ids]

    triage = make_stage("triage", kind=Kind.AGENT, batch_size=3)
    triage.run = run
    backend = FakeBackend(responder=responder)
    s = sched(db, units, tmp_path, [triage],
              {"triage": {"agent": {"tier": "medium"}}}, backend=backend)
    assert s.run()["triage"] == {"ok": 5}
    assert len(backend.calls) == 3  # 2 batches + 1 repair
    # The repair stays on the stage's tier, so the model credited with
    # the answer is the one that wrote its content.
    assert [c.tier for c in backend.calls] == ["medium"] * 3

    rows = db.query("SELECT * FROM stage_result WHERE stage='triage'")
    fixable = sorted(json.loads(r["data"])["fixable"] for r in rows)
    assert fixable == ["no", "no", "yes", "yes", "yes"]
    for r in rows:
        assert r["backend"] == "fake" and r["model"] == "fake-m"
        assert json.loads(r["usage"])["input_tokens"] > 0

    first = sorted(units.items)[0]
    item = units.items[first]
    adir = (tmp_path / "work" / item["source"] / item["version"]
            / item["arch"] / "triage" / "attempt-1")
    assert (adir / "prompt.md").exists()
    assert (adir / "transcript.jsonl").exists()
    assert json.loads((adir / "usage.json").read_text())["ok"]
    starts = db.query("SELECT 1 FROM event WHERE type='agent_call_start'")
    assert len(starts) == 2


def test_tier_escalates_after_loops(db, units, tmp_path):
    tiers = []

    def run_dev(self, ctx, unit_ids):
        (uid,) = unit_ids
        res = ctx.run_agent(uid, "fix it")
        tiers.append(res.model)
        return [StageResult(uid, Status.OK, {"n": len(tiers)})]

    dev = make_stage("dev", kind=Kind.AGENT)
    dev.run = run_dev

    def review(ctx, uid):
        return StageResult(uid, Status.FAIL, {})

    conf = {"dev": {"agent": {"tier": "medium",
                              "escalate_after_loops": 2}},
            "review": {"after": ["dev"],
                       "on_fail": {"goto": "dev", "max_loops": 2}}}
    one = Units([next(iter(units.items.values()))])
    sched(db, one, tmp_path, [dev, make_stage("review", fn=review)],
          conf).run()
    assert tiers == ["fake-m", "fake-m", "fake-l"]


@pytest.mark.parametrize("conf, registry, match", [
    ({"x": {}}, {}, "not registered"),
    ({"a": {"after": ["b"]}, "b": {"after": ["a"]}}, "ab", "cycle"),
    ({"a": {"after": ["zz"]}}, "a", "unknown or disabled"),
    ({"a": {}, "b": {"on_fail": {"goto": "a"}}}, "ab", "upstream"),
    ({"a": {"gate": "auto"}}, "a", "gate"),
    ({"a": {"agent": {"tier": "small"}}}, "a", "'agent' set"),
])
def test_pipeline_validation(conf, registry, match):
    reg = {n: make_stage(n) for n in registry} if isinstance(
        registry, str) else registry
    with pytest.raises(PipelineError, match=match):
        build({"stage": conf}, reg, "fake")


def test_disabled_stage_is_ignored():
    reg = {"a": make_stage("a")}
    p = build({"stage": {"a": {"enabled": False}}}, reg, "fake")
    assert list(p.specs) == []


def test_stage_status_wins_over_data_status(db, units, tmp_path):
    a = make_stage("a", fn=lambda ctx, uid: {"status": "attempted"})
    b = make_stage("b", fn=lambda ctx, uid: {
        "seen": ctx.upstream(uid, "a")["status"]})
    s = sched(db, units, tmp_path, [a, b], {"a": {}, "b": {"after": ["a"]}})
    assert s.run()["b"] == {"ok": 5}
    row = db.one("SELECT data FROM stage_result WHERE stage='b'")
    assert json.loads(row["data"])["seen"] == "ok"


def test_new_optional_agent_field_keeps_cache(db, units, tmp_path):
    from ftbfs.core.pipeline import AgentSpec
    from ftbfs.core.scheduler import _non_default

    assert _non_default(AgentSpec(backend="claude")) == {"backend": "claude"}
    assert _non_default(AgentSpec(backend="claude", effort="low")) == {
        "backend": "claude", "effort": "low"}


def test_cluster_cache_independent_of_selection(db, units, tmp_path):
    ids = list(units.items)
    for iid in ids:  # all five items share one cluster
        db.execute("UPDATE item SET cluster_id='c1' WHERE id=?", (iid,))
    rows = [dict(r, cluster_id="c1") for r in units.items.values()]
    item_stage = make_stage("a")
    cluster_stage = make_stage("summ", unit=UnitType.CLUSTER, fn=lambda ctx,
                               uid: {"members": len(ctx.units.children(
                                   UnitType.CLUSTER, uid))})
    conf = {"a": {}, "summ": {"after": ["a"]}}
    stages = [item_stage, cluster_stage]
    full = sched(db, Units(rows, db), tmp_path, stages, conf)
    full.run()
    assert calls("summ") == ["c1"]
    data = json.loads(db.one("SELECT data FROM stage_result"
                             " WHERE stage='summ'")["data"])
    assert data["members"] == 5

    CALLS.clear()
    subset = sched(db, Units(rows[:2], db), tmp_path, stages, conf)
    subset.run()
    assert calls("summ") == []  # same global membership -> cached
