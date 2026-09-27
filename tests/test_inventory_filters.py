import copy
import json

from ftbfs.filters import Filter, all_items, select
from ftbfs.inventory import item_id, store


def test_store_and_diff(db, snapshot, tmp_path):
    d1 = store(db, snapshot, tmp_path / "s1.json")
    assert len(d1.new) == 2771 and d1.gone == [] and d1.active == 0

    snap2 = copy.deepcopy(snapshot)
    fixed = snap2.packages.pop(0)  # this package got fixed
    d2 = store(db, snap2, tmp_path / "s2.json")
    gone_ids = {item_id(fixed.source, v.version, b.arch)
                for v in fixed.versions for b in v.builds}
    assert set(d2.gone) == gone_ids
    assert d2.new == []

    d3 = store(db, snapshot, tmp_path / "s3.json")  # it regressed
    assert set(d3.new) == gone_ids
    lifecycles = {r["lifecycle"] for r in db.query(
        "SELECT lifecycle FROM item WHERE source=?", (fixed.source,))}
    assert lifecycles == {"new"}
    events = [json.loads(r["payload"]) for r in db.query(
        "SELECT payload FROM event WHERE type='snapshot' ORDER BY id")]
    assert [e["gone"] for e in events] == [0, len(gone_ids), 0]


def test_filter_defaults_and_reasons(db, snapshot, tmp_path):
    store(db, snapshot, tmp_path / "s.json")
    flt = Filter.from_dict({"states": ["F"]})
    assert flt.states == ["FAILEDTOBUILD"]
    rows = select(db, flt)
    assert len(rows) == 1059
    assert all(r["component"] == "universe" for r in rows)
    assert all(json.loads(r["lp_bugs"]) == [] for r in rows)

    ceph = all_items(db, "WHERE i.source = 'ceph'")[0]
    reasons = flt.explain(ceph)
    assert "component main not in ['universe']" in reasons
    assert any("LP: #2099865" in r for r in reasons)


def test_filter_arch_pocket_glob_limit(db, snapshot, tmp_path):
    store(db, snapshot, tmp_path / "s.json")
    flt = Filter.from_dict({"arches": ["amd64"], "pockets": ["proposed"],
                            "sources": ["python-*"], "limit": 3})
    rows = select(db, flt)
    assert 0 < len(rows) <= 3
    for r in rows:
        assert r["arch"] == "amd64" and r["pocket"] == "proposed"
        assert r["source"].startswith("python-")


def test_unknown_filter_key_rejected():
    import pytest

    with pytest.raises(ValueError, match="unknown filter keys"):
        Filter.from_dict({"arch": ["amd64"]})
