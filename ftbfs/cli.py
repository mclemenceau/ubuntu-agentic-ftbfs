"""ftbfs command line."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import Counter
from pathlib import Path

from .app import App
from .db import PENDING_GATES


def _filter_args(p: argparse.ArgumentParser) -> None:
    g = p.add_argument_group("selection (overrides config.toml [filter])")
    g.add_argument("--profile", help="named filter profile from config")
    g.add_argument("--component", action="append", dest="components")
    g.add_argument("--state", action="append", dest="states",
                   help="F, M, X, U, C or full name")
    g.add_argument("--arch", action="append", dest="arches")
    g.add_argument("--pocket", action="append", dest="pockets",
                   choices=["release", "proposed"])
    g.add_argument("--packageset", action="append", dest="packagesets")
    g.add_argument("--team", action="append", dest="teams")
    g.add_argument("--source", action="append", dest="sources",
                   help="source name or glob")
    g.add_argument("--include-bugged", action="store_true",
                   help="do not skip packages with an LP bug")
    g.add_argument("--include-gone", action="store_true")
    g.add_argument("--all-components", action="store_true")
    g.add_argument("--all-states", action="store_true")
    g.add_argument("--limit", type=int)
    g.add_argument("--sample-clusters", type=int,
                   help="random whole clusters (all their items)")
    g.add_argument("--seed", type=int)


def _filter(app: App, a: argparse.Namespace):
    overrides = {
        k: getattr(a, k)
        for k in ("components", "states", "arches", "pockets",
                  "packagesets", "teams", "sources", "limit",
                  "sample_clusters", "seed")
    }
    if a.all_components:
        overrides["components"] = []
    if a.all_states:
        overrides["states"] = []
    if a.include_bugged:
        overrides["skip_lp_bug"] = False
    if a.include_gone:
        overrides["include_gone"] = True
    return app.config.make_filter(a.profile, **overrides)


def cmd_ingest(app: App, a) -> None:
    diff = app.ingest(Path(a.file) if a.file else None)
    print(f"snapshot {diff.snapshot_id}: {diff.active} unchanged,"
          f" {len(diff.new)} new, {len(diff.gone)} gone")


def cmd_export(app: App, a) -> None:
    data = json.dumps(app.export(_filter(app, a)), indent=1)
    if a.out:
        Path(a.out).write_text(data)
        print(f"wrote {a.out}")
    else:
        print(data)


def cmd_list(app: App, a) -> None:
    rows = app.select(_filter(app, a))
    by = Counter(r[a.by] for r in rows) if a.by else None
    if by:
        for k, n in by.most_common():
            print(f"{n:6}  {k}")
    else:
        for r in rows:
            print(f"{r['id']:60} {r['state']:14} {r['component']}")
    print(f"{len(rows)} items, {len({r['source'] for r in rows})} packages",
          file=sys.stderr)


def cmd_run(app: App, a) -> None:
    if a.ingest:
        cmd_ingest(app, a)
    res = app.run(_filter(app, a), only=a.stage, until=a.until,
                  trigger=a.trigger)
    print(f"run {res['run_id']} {res['status']}: {res['items']} items")
    for stage, counts in res["totals"].items():
        print(f"  {stage:22} " + ", ".join(
            f"{k}={v}" for k, v in sorted(counts.items())))
    if not res["totals"]:
        print("  nothing to do (all cached, gated or not eligible;"
              " see `ftbfs why`)")


def cmd_clusters(app: App, a) -> None:
    rows = [r for r in app.select(_filter(app, a)) if r["cluster_id"]]
    if not rows:
        sys.exit("no classified items; run `ftbfs run` first")
    clusters: dict[str, list] = {}
    for r in rows:
        clusters.setdefault(r["cluster_id"], []).append(r)
    known = sum(1 for r in rows if r["class"] != "unknown")
    print(f"{len(rows)} classified items, {len(clusters)} clusters;"
          f" rules matched {known} ({100 * known / len(rows):.0f}%)")
    fam = Counter(r["family"] or "unknown" for r in rows)
    print("by family: " + ", ".join(f"{k}={v}" for k, v in
                                    fam.most_common()))
    multi = [c for c in clusters.values() if len(c) > 1]
    print(f"clusters with >1 item: {len(multi)} covering"
          f" {sum(len(c) for c in multi)} items\n")
    ordered = sorted(clusters.items(), key=lambda kv: -len(kv[1]))
    for cid, members in ordered:
        if len(members) < a.min_size:
            continue
        pkgs = sorted({m["source"] for m in members})
        ex = app.db.one(
            "SELECT data FROM stage_result WHERE unit_id=? AND"
            " stage='excerpt' ORDER BY id DESC LIMIT 1",
            (members[0]["id"],))
        key = ""
        if ex:
            keys = json.loads(ex["data"]).get("key_lines") or [""]
            key = keys[0]
        print(f"{len(members):4} items {len(pkgs):4} pkgs  {cid}")
        print(f"      e.g. {', '.join(pkgs[:6])}"
              f"{' ...' if len(pkgs) > 6 else ''}")
        print(f"      {key[:150]}")


def cmd_signals(app: App, a) -> None:
    """Packages per deterministic facts signal."""
    sources = {r["source"] for r in app.select(_filter(app, a))}
    rows = app.db.query(
        "SELECT unit_id, data FROM (SELECT unit_id, data, MAX(id)"
        " FROM stage_result WHERE stage='facts' AND status='ok'"
        " GROUP BY unit_id)")
    facts = [json.loads(r["data"]) for r in rows if r["unit_id"] in sources]
    if not facts:
        sys.exit("no facts yet; run `ftbfs run`")
    by: dict[str, list] = {}
    for f in facts:
        for sig in f["signals"]:
            by.setdefault(sig, []).append(f)
    print(f"{len(facts)} packages with facts")
    for sig, fs in sorted(by.items(), key=lambda kv: -len(kv[1])):
        if a.signal and sig not in a.signal:
            continue
        print(f"\n{sig}: {len(fs)}")
        if not (a.signal or a.verbose):
            continue
        for f in sorted(fs, key=lambda f: f["source"]):
            bugs = f["debian_bugs"]["open"] + f["debian_bugs"]["fixed_newer"]
            bug = f"  #{bugs[0]['id']} {bugs[0]['title'][:60]}" if bugs \
                else ""
            print(f"  {f['source']:28} {f['ubuntu']['newest_failing']:24}"
                  f" debian {f['debian']['newest'] or '-':20}{bug}")


def cmd_verdicts(app: App, a) -> None:
    """Triage and diagnosis per cluster of the selection."""
    rows = app.select(_filter(app, a))
    clusters: dict[str, list] = {}
    for r in rows:
        if r["cluster_id"]:
            clusters.setdefault(r["cluster_id"], []).append(r)

    def latest(uid, stage):
        row = app.db.one(
            "SELECT status, data, cost, model FROM stage_result WHERE"
            " unit_id=? AND stage=? ORDER BY id DESC LIMIT 1", (uid, stage))
        return row

    actions: Counter = Counter()
    cost = 0.0
    for cid, members in sorted(clusters.items(), key=lambda kv: -len(kv[1])):
        t = latest(cid, "triage")
        if t is None:
            continue
        tv = json.loads(t["data"])
        cost += t["cost"] or 0
        actions[tv.get("action", t["status"])] += 1
        pkgs = sorted({m["source"] for m in members})
        if a.action and tv.get("action") not in a.action:
            continue
        print(f"{cid}  ({len(members)} items, {len(pkgs)} pkgs:"
              f" {', '.join(pkgs[:4])}{' ...' if len(pkgs) > 4 else ''})")
        print(f"  triage   [{tv.get('decided_by', '?')}] {tv.get('action')}"
              f" fixable={tv.get('fixable')} obvious={tv.get('obvious')}"
              f" conf={tv.get('confidence')}  {tv.get('summary', '')}")
        d = latest(cid, "diagnose")
        if d is not None and d["status"] == "ok":
            dv = json.loads(d["data"])
            cost += d["cost"] or 0
            print(f"  diagnose {dv['fix_kind']} risk={dv['risk']}"
                  f" conf={dv['confidence']}  {dv['root_cause'][:300]}")
            print(f"           fix: {dv['fix_strategy'][:300]}")
        print()
    print("actions: " + ", ".join(f"{k}={v}" for k, v in
                                  actions.most_common()))
    print(f"recorded LLM cost for these clusters: ${cost:.3f}")


def cmd_report(app: App, a) -> None:
    """investigation.md per selected source version."""
    pairs = sorted({(r["source"], r["version"])
                    for r in app.select(_filter(app, a))})
    if not pairs:
        sys.exit("nothing selected")
    reporter = app.reporter
    for source, version in pairs:
        if a.stdout:
            print(reporter.report(source, version))
        else:
            path = reporter.write(app.config.work_dir, source, version)
            print(path.relative_to(app.config.root))


def cmd_serve(app: App, a) -> None:
    import uvicorn

    from .web.app import create_app

    if a.host not in ("127.0.0.1", "localhost", "::1"):
        print("warning: the UI has no authentication; anyone who can"
              f" reach {a.host}:{a.port} can approve gates",
              file=sys.stderr)
    uvicorn.run(create_app(app.config.root), host=a.host, port=a.port,
                log_level="warning")


def cmd_stages(app: App, a) -> None:
    registry = app.registry
    pipeline = app.pipeline
    for name, cls in sorted(registry.items()):
        spec = pipeline.specs.get(name)
        state = "enabled" if spec else "not in pipeline"
        print(f"{name:22} {cls.kind:13} {cls.unit:8} v{cls.version:4}"
              f" {state}")
        if spec:
            extra = []
            if spec.after:
                extra.append(f"after={spec.after}")
            if spec.when:
                extra.append(f"when={spec.when!r}")
            if spec.gate:
                extra.append(f"gate={spec.gate}")
            if spec.agent:
                extra.append(f"agent={spec.agent.backend}/"
                             f"{spec.agent.tier}")
            if spec.on_fail:
                extra.append(f"on_fail->{spec.on_fail.goto}"
                             f" x{spec.on_fail.max_loops}")
            extra.extend(spec.notes)
            if extra:
                print("    " + "  ".join(extra))
    if not registry:
        print("no stages registered yet")


def _resolve_items(app: App, target: str) -> list[str]:
    rows = app.db.query(
        "SELECT id FROM item WHERE id=? OR source=? ORDER BY id",
        (target, target),
    )
    if not rows:
        sys.exit(f"no item or source named {target!r}")
    return [r["id"] for r in rows]


def cmd_why(app: App, a) -> None:
    flt = _filter(app, a)
    for iid, info in app.explain(_resolve_items(app, a.target), flt).items():
        print(iid)
        if info["filtered_out"]:
            for reason in info["filtered_out"]:
                print(f"  excluded: {reason}")
            continue
        if not info["stages"]:
            print("  selected; no stages in pipeline yet")
        for stage, reason in info["stages"]:
            print(f"  {stage:22} {reason}")


def _fmt_event(e) -> str:
    payload = json.loads(e["payload"])
    brief = ", ".join(f"{k}={v}" for k, v in payload.items()
                      if k not in ("data", "traceback") and v is not None)
    where = " ".join(x for x in (e["stage"], e["unit"]) if x)
    return f"{e['ts']} #{e['id']:<6} {e['type']:16} {where}  {brief}"[:220]


def cmd_show(app: App, a) -> None:
    ids = _resolve_items(app, a.target)
    source = ids[0].split("/")[0]
    events = app.db.query(
        f"SELECT * FROM event WHERE unit IN ({','.join('?' * len(ids))})"
        " OR unit=? ORDER BY id", (*ids, source),
    )
    for e in events:
        print(_fmt_event(e))
    results = app.db.query(
        f"SELECT * FROM stage_result WHERE unit_id IN"
        f" ({','.join('?' * len(ids))}) OR unit_id=? ORDER BY id",
        (*ids, source),
    )
    if results:
        print("\nresults:")
    for r in results:
        print(f"  {r['ts']} {r['stage']:20} {r['unit_id']:50}"
              f" {r['status']:8} attempt={r['attempt']}")


def cmd_tail(app: App, a) -> None:
    where, params = ["id > ?"], [0]
    if a.run:
        where.append("run_id = ?")
        params.append(a.run)
    if a.unit:
        where.append("(unit = ? OR unit LIKE ?)")
        params += [a.unit, f"{a.unit}/%"]
    if a.stage:
        where.append("stage = ?")
        params.append(a.stage)
    last = app.db.one("SELECT MAX(id) AS m FROM event")["m"] or 0
    params[0] = max(0, last - a.n)
    while True:
        rows = app.db.query(
            f"SELECT * FROM event WHERE {' AND '.join(where)} ORDER BY id",
            params,
        )
        for e in rows:
            print(_fmt_event(e), flush=True)
            params[0] = e["id"]
        if not a.follow:
            return
        time.sleep(1)


def cmd_status(app: App, a) -> None:
    app.reap_stale_runs()
    runs = app.db.query("SELECT * FROM run ORDER BY id DESC LIMIT 5")
    snap = app.db.one("SELECT * FROM snapshot ORDER BY id DESC LIMIT 1")
    if snap:
        print(f"latest snapshot #{snap['id']} {snap['series']}"
              f" fetched {snap['fetched_at']}")
    life = app.db.query(
        "SELECT lifecycle, COUNT(*) AS n FROM item GROUP BY lifecycle")
    print("items: " + ", ".join(f"{r['lifecycle']}={r['n']}" for r in life))
    print("\nruns:")
    for r in runs:
        print(f"  #{r['id']} {r['status']:9} started {r['started']}"
              f" finished {r['finished'] or '-'}"
              f"{'  control=' + r['control'] if r['control'] else ''}")
    stages = app.db.query(
        "SELECT stage, status, COUNT(*) AS n, SUM(cost) AS cost FROM"
        " (SELECT stage, status, cost, MAX(id) FROM stage_result"
        "  GROUP BY unit_id, stage) GROUP BY stage, status ORDER BY stage"
    )
    if stages:
        print("\nlatest result per unit:")
        for r in stages:
            cost = f"  ${r['cost']:.3f}" if r["cost"] else ""
            print(f"  {r['stage']:22} {r['status']:12} {r['n']:6}{cost}")
    gates = app.db.query(
        f"SELECT stage, COUNT(*) AS n FROM ({PENDING_GATES})"
        " GROUP BY stage")
    if gates:
        print("\nwaiting for approval:")
        for g in gates:
            print(f"  {g['stage']:22} {g['n']}")
    total = app.db.one("SELECT SUM(cost) AS c FROM stage_result")["c"]
    if total:
        print(f"\ntotal LLM cost recorded: ${total:.2f}")


def cmd_approve(app: App, a) -> None:
    decision = "rejected" if a.reject else "approved"
    try:
        units = app.approve(a.stage, a.units, decision, "cli", a.note)
    except ValueError as e:
        sys.exit(str(e))
    for uid in units:
        print(f"{decision}: {a.stage} {uid}")


def cmd_retry(app: App, a) -> None:
    try:
        app.retry(a.stage, a.units, "cli")
    except ValueError as e:
        sys.exit(str(e))
    for uid in a.units:
        print(f"retry requested: {a.stage} {uid}")


def cmd_lp_login(app: App, a) -> None:
    """Interactive, one time: authorize this tool on Launchpad."""
    from .builder.ppa import login

    creds = app.config.state_dir / "lp-credentials"
    creds.parent.mkdir(parents=True, exist_ok=True)
    lp = login(creds)
    print(f"logged in as {lp.me.name}; credentials in {creds}")


def cmd_control(app: App, a) -> None:
    try:
        run_id = app.control(a.action, "cli", a.run)
    except LookupError as e:
        sys.exit(str(e))
    print(f"run {run_id}: {a.action}")


def main(argv: list[str] | None = None) -> None:
    p = argparse.ArgumentParser(prog="ftbfs")
    p.add_argument("--root", default=".", help="project directory")
    sub = p.add_subparsers(dest="cmd", required=True)

    s = sub.add_parser("ingest", help="fetch/parse the FTBFS page")
    s.add_argument("--file", help="parse a saved page (.html or .html.gz)")
    s.set_defaults(func=cmd_ingest)

    s = sub.add_parser("export", help="selected items as JSON")
    _filter_args(s)
    s.add_argument("--out")
    s.set_defaults(func=cmd_export)

    s = sub.add_parser("list", help="list selected items")
    _filter_args(s)
    s.add_argument("--by", choices=["arch", "component", "source",
                                    "state", "pocket"])
    s.set_defaults(func=cmd_list)

    s = sub.add_parser("run", help="run the pipeline on selected items")
    _filter_args(s)
    s.add_argument("--ingest", action="store_true",
                   help="fetch a fresh snapshot first")
    s.add_argument("--file", help="with --ingest: parse a saved page")
    s.add_argument("--stage", action="append",
                   help="only run these stages")
    s.add_argument("--until", help="run up to and including this stage")
    s.add_argument("--trigger", default="cli", help=argparse.SUPPRESS)
    s.set_defaults(func=cmd_run)

    s = sub.add_parser("clusters", help="failure clusters and rule hits")
    _filter_args(s)
    s.add_argument("--min-size", type=int, default=2)
    s.set_defaults(func=cmd_clusters)

    s = sub.add_parser("signals", help="packages per Debian/upstream"
                       " facts signal")
    _filter_args(s)
    s.add_argument("--signal", action="append",
                   help="only list this signal (repeatable)")
    s.add_argument("-v", "--verbose", action="store_true",
                   help="list packages for every signal")
    s.set_defaults(func=cmd_signals)

    s = sub.add_parser("verdicts", help="triage/diagnosis per cluster")
    _filter_args(s)
    s.add_argument("--action", action="append")
    s.set_defaults(func=cmd_verdicts)

    s = sub.add_parser("report", help="write investigation.md per"
                       " selected source version")
    _filter_args(s)
    s.add_argument("--stdout", action="store_true",
                   help="print instead of writing work/<src>/<ver>/")
    s.set_defaults(func=cmd_report)

    s = sub.add_parser("serve", help="web UI: live runs, gates, reports")
    s.add_argument("--host", default="127.0.0.1")
    s.add_argument("--port", type=int, default=8047)
    s.set_defaults(func=cmd_serve)

    s = sub.add_parser("stages", help="registered stages and pipeline")
    s.set_defaults(func=cmd_stages)

    s = sub.add_parser("why", help="why an item is (not) processed")
    _filter_args(s)
    s.add_argument("target", help="source name or item id")
    s.set_defaults(func=cmd_why)

    s = sub.add_parser("show", help="event timeline for a package/item")
    s.add_argument("target")
    s.set_defaults(func=cmd_show)

    s = sub.add_parser("tail", help="print (and follow) events")
    s.add_argument("-f", "--follow", action="store_true")
    s.add_argument("-n", type=int, default=30)
    s.add_argument("--run", type=int)
    s.add_argument("--unit")
    s.add_argument("--stage")
    s.set_defaults(func=cmd_tail)

    s = sub.add_parser("status", help="runs, stage counts, gates, cost")
    s.set_defaults(func=cmd_status)

    s = sub.add_parser("approve", help="approve/reject a gated stage")
    s.add_argument("stage")
    s.add_argument("units", nargs="+")
    s.add_argument("--reject", action="store_true")
    s.add_argument("--note")
    s.set_defaults(func=cmd_approve)

    s = sub.add_parser("retry", help="force a stage to re-run for units")
    s.add_argument("stage")
    s.add_argument("units", nargs="+")
    s.set_defaults(func=cmd_retry)

    s = sub.add_parser("lp-login", help="authorize Launchpad access"
                       " (interactive, once)")
    s.set_defaults(func=cmd_lp_login)

    s = sub.add_parser("control", help="pause/resume/cancel a run")
    s.add_argument("action", choices=["pause", "resume", "cancel"])
    s.add_argument("--run", type=int)
    s.set_defaults(func=cmd_control)

    a = p.parse_args(argv)
    app = App(Path(a.root).resolve())
    a.func(app, a)
