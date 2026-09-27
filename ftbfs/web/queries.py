"""Read models for the web UI. All SQL the pages need lives here."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from pathlib import Path

from ..core.pipeline import Pipeline
from ..db import DB

# The latest result per (unit, stage): what the pipeline currently thinks.
LATEST = """
    SELECT * FROM stage_result WHERE id IN
      (SELECT MAX(id) FROM stage_result GROUP BY unit_id, stage)
"""
PROBLEMS = ("error", "needs_human")


def payload(row) -> dict:
    return json.loads(row["payload"]) if row["payload"] else {}


def age_s(ts: str | None) -> int | None:
    if not ts:
        return None
    then = datetime.fromisoformat(ts)
    return int((datetime.now(UTC) - then).total_seconds())


# -- pipeline ------------------------------------------------------------

def stage_counts(db: DB) -> dict[str, dict[str, int]]:
    out: dict[str, dict[str, int]] = {}
    for r in db.query(f"SELECT stage, status, COUNT(*) AS n FROM ({LATEST})"
                      " GROUP BY stage, status"):
        out.setdefault(r["stage"], {})[r["status"]] = r["n"]
    for r in db.query("SELECT stage, COUNT(*) AS n FROM gate"
                      " WHERE decision='pending' GROUP BY stage"):
        out.setdefault(r["stage"], {})["gate"] = r["n"]
    return out


def dag_layers(pipeline: Pipeline) -> list[list[str]]:
    """Stages grouped by longest path from a root, for a left-to-right
    drawing of the DAG."""
    depth: dict[str, int] = {}
    for spec in pipeline:  # topological order
        depth[spec.name] = 1 + max((depth[d] for d in spec.after),
                                   default=-1)
    layers: list[list[str]] = [[] for _ in range(max(depth.values(),
                                                      default=-1) + 1)]
    for name, d in depth.items():
        layers[d].append(name)
    return layers


# -- overview ------------------------------------------------------------

def snapshots(db: DB) -> list:
    return db.query("SELECT * FROM snapshot ORDER BY id DESC")


def lifecycle_counts(db: DB) -> dict[str, int]:
    return {r["lifecycle"]: r["n"] for r in db.query(
        "SELECT lifecycle, COUNT(*) AS n FROM item GROUP BY lifecycle")}


def runs(db: DB, limit: int = 20) -> list[dict]:
    rows = db.query(
        "SELECT r.*, (SELECT COALESCE(SUM(cost), 0) FROM stage_result"
        " WHERE run_id = r.id) AS cost, (SELECT COUNT(*) FROM stage_result"
        " WHERE run_id = r.id) AS results FROM run r ORDER BY id DESC"
        " LIMIT ?", (limit,))
    return [dict(r) for r in rows]


def total_cost(db: DB) -> float:
    return db.one("SELECT COALESCE(SUM(cost), 0) AS c FROM stage_result")["c"]


# -- live run ------------------------------------------------------------

def run_state(db: DB, run_id: int) -> dict:
    """Per-stage counters and what is in flight right now."""
    run = db.one("SELECT * FROM run WHERE id=?", (run_id,))
    if run is None:
        raise LookupError(run_id)
    stages: dict[str, dict[str, int]] = {}
    open_units: dict[tuple[str, str], dict] = {}
    agents: dict[tuple[str, str], dict] = {}
    builds: dict[tuple[str, str], dict] = {}
    for e in db.query(
        "SELECT * FROM event WHERE run_id=? AND type IN ('unit_start',"
        " 'unit_end', 'agent_call_start', 'agent_call_end', 'build_start',"
        " 'build_end') ORDER BY id", (run_id,)):
        p = payload(e)
        key = (e["stage"], p.get("unit") or e["unit"])
        per = stages.setdefault(e["stage"], {})
        if e["type"] == "unit_start":
            open_units[key] = {"since": e["ts"]}
        elif e["type"] == "unit_end":
            open_units.pop(key, None)
            per[p["status"]] = per.get(p["status"], 0) + 1
        elif e["type"] == "agent_call_start":
            agents[key] = {"stage": e["stage"], "unit": key[1],
                           "since": e["ts"], "model": p.get("model"),
                           "tier": p.get("tier"),
                           "attempt_dir": p.get("attempt_dir")}
        elif e["type"] == "agent_call_end":
            agents.pop(key, None)
        elif e["type"] == "build_start":
            builds[key] = {"stage": e["stage"], "unit": key[1],
                           "since": e["ts"], "where": p.get("where")}
        elif e["type"] == "build_end":
            builds.pop(key, None)
    live = run["status"] == "running"
    for (stage, _), _u in open_units.items():
        if live:
            per = stages.setdefault(stage, {})
            per["running"] = per.get("running", 0) + 1
    for d in (*agents.values(), *builds.values()):
        d["elapsed_s"] = age_s(d["since"])
    cost = db.one("SELECT COALESCE(SUM(cost), 0) AS c FROM stage_result"
                  " WHERE run_id=?", (run_id,))["c"]
    return {
        "run": dict(run),
        "filter": json.loads(run["filter"]),
        "stages": stages,
        "agents": list(agents.values()) if live else [],
        "builds": list(builds.values()) if live else [],
        "cost": cost,
        "pending_polls": db.one(
            f"SELECT COUNT(*) AS n FROM ({LATEST}) WHERE status='pending'"
        )["n"],
        "gates": db.one("SELECT COUNT(*) AS n FROM gate"
                        " WHERE decision='pending'")["n"],
    }


def events(db: DB, *, after: int = 0, run_id: int | None = None,
           unit: str | None = None, stage: str | None = None,
           limit: int = 200) -> list:
    where, params = ["id > ?"], [after]
    if run_id:
        where.append("run_id = ?")
        params.append(run_id)
    if unit:
        where.append("(unit = ? OR unit LIKE ?)")
        params += [unit, f"{unit}/%"]
    if stage:
        where.append("stage = ?")
        params.append(stage)
    return db.query(f"SELECT * FROM event WHERE {' AND '.join(where)}"
                    " ORDER BY id LIMIT ?", (*params, limit))


def last_event_id(db: DB) -> int:
    return db.one("SELECT COALESCE(MAX(id), 0) AS m FROM event")["m"]


# -- items, packages, clusters -------------------------------------------

def latest_by_unit(db: DB, unit_ids: list[str]) -> dict[str, dict]:
    """{unit_id: {stage: row}} for the latest results of these units."""
    out: dict[str, dict] = {}
    for i in range(0, len(unit_ids), 500):
        chunk = unit_ids[i:i + 500]
        marks = ",".join("?" * len(chunk))
        for r in db.query(
            f"SELECT * FROM stage_result WHERE id IN (SELECT MAX(id) FROM"
            f" stage_result WHERE unit_id IN ({marks})"
            f" GROUP BY unit_id, stage)", chunk):
            out.setdefault(r["unit_id"], {})[r["stage"]] = r
    return out


def timeline(db: DB, source: str, version: str | None = None) -> list:
    """Every event for a package across runs: its items (optionally one
    version), the package unit and its clusters."""
    like = f"{source}/{version}/%" if version else f"{source}/%"
    clusters = [r["cluster_id"] for r in db.query(
        "SELECT DISTINCT cluster_id FROM item WHERE id LIKE ? AND"
        " cluster_id IS NOT NULL", (like,))]
    marks = ",".join("?" * len(clusters))
    extra = f" OR unit IN ({marks})" if clusters else ""
    return db.query(
        f"SELECT * FROM event WHERE unit LIKE ? OR unit = ?{extra}"
        " ORDER BY id", (like, source, *clusters))


def clusters(db: DB) -> list[dict]:
    rows = db.query(
        "SELECT cluster_id, class, family, COUNT(*) AS items,"
        " COUNT(DISTINCT source) AS packages, GROUP_CONCAT(DISTINCT source)"
        " AS sources FROM item WHERE cluster_id IS NOT NULL AND"
        " lifecycle != 'gone' GROUP BY cluster_id ORDER BY items DESC")
    latest = latest_by_unit(db, [r["cluster_id"] for r in rows])
    out = []
    for r in rows:
        d = dict(r)
        res = latest.get(r["cluster_id"], {})
        for stage in ("triage", "diagnose"):
            row = res.get(stage)
            d[stage] = ({**json.loads(row["data"]), "status": row["status"]}
                        if row else None)
        out.append(d)
    return out


# -- facts signals ------------------------------------------------------

def facts(db: DB, sources: list[str] | None = None) -> dict[str, dict]:
    """Latest ok facts per source package, optionally only these."""
    rows = db.query(
        "SELECT unit_id, data FROM stage_result WHERE id IN (SELECT MAX(id)"
        " FROM stage_result WHERE stage='facts' AND status='ok'"
        " GROUP BY unit_id)")
    keep = set(sources) if sources is not None else None
    return {r["unit_id"]: json.loads(r["data"]) for r in rows
            if keep is None or r["unit_id"] in keep}


def signal_counts(facts_by_source: dict[str, dict]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for f in facts_by_source.values():
        for sig in f["signals"]:
            counts[sig] = counts.get(sig, 0) + 1
    return dict(sorted(counts.items(), key=lambda kv: (-kv[1], kv[0])))


# -- gates and attention -------------------------------------------------

def gates(db: DB, decision: str = "pending", limit: int = 500) -> list:
    return db.query("SELECT * FROM gate WHERE decision=? ORDER BY stage,"
                    " ts DESC LIMIT ?", (decision, limit))


def attention(db: DB) -> dict:
    problems = db.query(
        f"SELECT * FROM ({LATEST}) WHERE status IN"
        f" ({','.join('?' * len(PROBLEMS))}) ORDER BY ts DESC", PROBLEMS)
    exhausted = db.query(
        "SELECT * FROM event WHERE type='loop_exhausted' ORDER BY id DESC"
        " LIMIT 100")
    failed_runs = db.query(
        "SELECT * FROM run WHERE status IN ('failed', 'interrupted')"
        " ORDER BY id DESC LIMIT 20")
    return {"problems": problems, "exhausted": exhausted,
            "failed_runs": failed_runs}


# -- cost ledger ---------------------------------------------------------

def ledger(db: DB) -> dict:
    where = "WHERE cost IS NOT NULL OR usage IS NOT NULL"
    by_stage_model = db.query(
        "SELECT stage, COALESCE(backend, '-') AS backend,"
        " COALESCE(model, '-') AS model, COUNT(*) AS calls,"
        " COALESCE(SUM(cost), 0) AS cost,"
        " SUM(json_extract(usage, '$.input_tokens')) AS input,"
        " SUM(json_extract(usage, '$.output_tokens')) AS output,"
        " SUM(json_extract(usage, '$.cache_read_tokens')) AS cache_read"
        f" FROM stage_result {where} GROUP BY stage, backend, model"
        " ORDER BY cost DESC")
    by_run = db.query(
        "SELECT run_id, COUNT(*) AS calls, COALESCE(SUM(cost), 0) AS cost"
        f" FROM stage_result {where} GROUP BY run_id ORDER BY run_id DESC"
        " LIMIT 30")
    # Item and package units roll up to their source; clusters stay apart.
    by_unit = db.query(
        "SELECT CASE WHEN unit_type IN ('item', 'package') THEN"
        " CASE WHEN instr(unit_id, '/') THEN substr(unit_id, 1,"
        " instr(unit_id, '/') - 1) ELSE unit_id END ELSE unit_id END"
        " AS unit, unit_type = 'cluster' AS is_cluster, COUNT(*) AS calls,"
        f" COALESCE(SUM(cost), 0) AS cost FROM stage_result {where}"
        " GROUP BY unit, is_cluster ORDER BY cost DESC LIMIT 30")
    return {"by_stage_model": by_stage_model, "by_run": by_run,
            "by_unit": by_unit, "total": total_cost(db)}


# -- snapshot deltas -----------------------------------------------------

def _snapshot_items(path: str) -> dict[str, dict]:
    data = json.loads(Path(path).read_text())
    out = {}
    for p in data["packages"]:
        for v in p["versions"]:
            for b in v["builds"]:
                out[f"{p['source']}/{v['version']}/{b['arch']}"] = {
                    "source": p["source"], "version": v["version"],
                    "arch": b["arch"], "state": b["state"],
                    "component": p["component"]}
    return out


def snapshot_diff(db: DB, a: int, b: int) -> dict:
    """What changed from snapshot a to snapshot b. `regressed` items
    failed before a, were gone in a, and fail again in b."""
    sa = db.one("SELECT * FROM snapshot WHERE id=?", (a,))
    sb = db.one("SELECT * FROM snapshot WHERE id=?", (b,))
    if sa is None or sb is None:
        raise LookupError("unknown snapshot")
    ia, ib = _snapshot_items(sa["path"]), _snapshot_items(sb["path"])
    appeared = sorted(set(ib) - set(ia))
    first_seen = {}
    for i in range(0, len(appeared), 500):
        chunk = appeared[i:i + 500]
        first_seen.update({r["id"]: r["first_seen"] for r in db.query(
            f"SELECT id, first_seen FROM item WHERE id IN"
            f" ({','.join('?' * len(chunk))})", chunk)})
    regressed = [i for i in appeared if first_seen.get(i, b) < a]
    new = [i for i in appeared if i not in set(regressed)]
    changed = [(i, ia[i]["state"], ib[i]["state"])
               for i in sorted(set(ia) & set(ib))
               if ia[i]["state"] != ib[i]["state"]]
    gone = sorted(set(ia) - set(ib))
    fixed_sources = sorted({ia[i]["source"] for i in gone}
                           - {v["source"] for v in ib.values()})
    return {"a": dict(sa), "b": dict(sb), "new": new, "gone": gone,
            "regressed": regressed, "changed": changed,
            "fixed_sources": fixed_sources, "count_a": len(ia),
            "count_b": len(ib)}
