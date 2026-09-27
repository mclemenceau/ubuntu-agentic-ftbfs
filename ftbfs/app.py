"""Wiring shared by the CLI and the web UI."""

from __future__ import annotations

import json
import os
import signal
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .agents import make_backend
from .agents.base import running_pid
from .config import Config, load_config
from .core.context import Paths, Units
from .core.pipeline import Pipeline
from .core.pipeline import load as load_pipeline
from .core.scheduler import Cancelled, Scheduler
from .core.stage import discover
from .db import DB, now
from .filters import Filter, all_items, select
from .ingest import fetch, parse, read_html
from .inventory import Diff, save_snapshot, store


class App:
    def __init__(self, root: Path):
        self.config: Config = load_config(root)
        self.db = DB(self.config.db_path)
        self._pipeline: Pipeline | None = None

    @property
    def registry(self):
        return discover(self.config.plugins_dir)

    @property
    def pipeline(self) -> Pipeline:
        if self._pipeline is None:
            self._pipeline = load_pipeline(
                self.config.pipeline_path, self.registry,
                self.config.default_backend,
            )
        return self._pipeline

    @property
    def reporter(self):
        from .report import Reporter

        return Reporter(self.db, self.pipeline, self.config.plugins_dir)

    def backends(self) -> dict:
        names = {s.agent.backend for s in self.pipeline if s.agent}
        return {n: make_backend(n, self.config.backends.get(n, {}))
                for n in names}

    # -- ingest -----------------------------------------------------------

    def ingest(self, file: Path | None = None) -> Diff:
        if file:
            data = read_html(file)
            source = str(file)
        else:
            data = fetch(self.config.source_url)
            source = self.config.source_url
        snap = parse(data, source_url=source)
        path = save_snapshot(snap, self.config.snapshots_dir)
        return store(self.db, snap, path)

    # -- selection --------------------------------------------------------

    def select(self, flt: Filter) -> list:
        return select(self.db, flt)

    def export(self, flt: Filter) -> dict:
        snap = self.db.one("SELECT * FROM snapshot ORDER BY id DESC LIMIT 1")
        packages: dict[str, dict] = {}
        for r in self.select(flt):
            pkg = packages.setdefault(r["source"], {
                "source": r["source"],
                "component": r["component"],
                "packagesets": json.loads(r["packagesets"]),
                "teams": json.loads(r["teams"]),
                "lp_bugs": json.loads(r["lp_bugs"]),
                "pts": r["pts"],
                "bts": r["bts"],
                "items": [],
            })
            pkg["items"].append({
                k: r[k] for k in ("id", "version", "pocket", "arch", "state",
                                  "build_url", "log_url", "finished_at",
                                  "note", "changed_by", "lifecycle")
            })
        return {
            "series": snap["series"] if snap else None,
            "fetched_at": snap["fetched_at"] if snap else None,
            "filter": flt.to_dict(),
            "package_count": len(packages),
            "item_count": sum(len(p["items"]) for p in packages.values()),
            "packages": list(packages.values()),
        }

    # -- runs -------------------------------------------------------------

    def scheduler(self, units: Units, run_id: int | None = None):
        return Scheduler(
            self.db, self.pipeline, units, self.backends(),
            Paths(self.config.root, self.config.work_dir,
                  self.config.cache_dir),
            self.config.concurrency, run_id,
        )

    def reap_stale_runs(self) -> list[int]:
        """Mark runs whose process died (killed, terminal closed, ...) as
        interrupted. Their finished work stays cached; a new run resumes.

        Runs without a pid (older schema) are reaped once they have been
        silent for STALE_AFTER.
        """
        host = socket.gethostname()
        cutoff = (datetime.now(UTC) - STALE_AFTER).isoformat(
            timespec="seconds")
        reaped = []
        for r in self.db.query(
            "SELECT id, pid, host, (SELECT MAX(ts) FROM event"
            " WHERE run_id = run.id) AS last FROM run"
            " WHERE status = 'running'"
        ):
            if r["pid"] is not None:
                dead = r["host"] == host and not _alive(r["pid"])
            else:
                dead = (r["last"] or "") < cutoff
            if dead:
                self.db.execute(
                    "UPDATE run SET status='interrupted', finished=?"
                    " WHERE id=?", (now(), r["id"]))
                self.db.event("run_interrupted", run_id=r["id"],
                              pid=r["pid"])
                reaped.append(r["id"])
        return reaped

    def run(self, flt: Filter, only: list[str] | None = None,
            until: str | None = None, trigger: str = "cli") -> dict:
        self.reap_stale_runs()
        items = self.select(flt)
        run_id = self.db.execute(
            "INSERT INTO run (started, status, filter, pipeline_hash,"
            " trigger, pid, host) VALUES (?, 'running', ?, ?, ?, ?, ?)",
            (now(), json.dumps(flt.to_dict()), self.pipeline.hash, trigger,
             os.getpid(), socket.gethostname()),
        ).lastrowid
        self.db.event("run_start", run_id=run_id, items=len(items),
                      stages=list(self.pipeline.specs))
        status = "done"
        totals: dict = {}
        try:
            totals = self.scheduler(Units(items, self.db), run_id).run(
                only, until)
        except Cancelled:
            status = "cancelled"
        except BaseException as e:
            status = "failed"
            self.db.event("error", run_id=run_id, error=repr(e))
            raise
        finally:
            self.db.execute(
                "UPDATE run SET status=?, finished=? WHERE id=?",
                (status, now(), run_id),
            )
            self.db.event("run_end", run_id=run_id, status=status,
                          totals=totals)
        return {"run_id": run_id, "status": status, "items": len(items),
                "totals": totals}

    def explain(self, item_ids: list[str], flt: Filter) -> dict:
        """Per item: filter reasons, then per-stage decisions."""
        rows = {r["id"]: r for r in all_items(self.db)}
        out = {}
        selected = self.select(flt)
        units = Units(selected, self.db)
        sched = self.scheduler(units)
        for iid in item_ids:
            row = rows[iid]
            reasons = flt.explain(row)
            if reasons:
                out[iid] = {"filtered_out": reasons, "stages": []}
                continue
            if iid not in units.items:
                out[iid] = {"filtered_out": ["beyond --limit"],
                            "stages": []}
                continue
            item = units.items[iid]
            uid_for = {"item": iid, "package": item["source"]}
            if item.get("cluster_id"):
                uid_for["cluster"] = item["cluster_id"]
            out[iid] = {"filtered_out": [],
                        "stages": sched.explain(uid_for)}
        return out


    # -- controls (shared by the CLI and the web UI; all are events) -----

    def approve(self, stage: str, targets: list[str], decision: str,
                by: str, note: str | None = None) -> list[str]:
        """Record a gate decision. For item stages a source name expands
        to its items waiting at this gate, or if none are waiting, to all
        its active items. Returns the unit ids decided."""
        if decision not in ("approved", "rejected"):
            raise ValueError(f"bad decision {decision!r}")
        spec = self.pipeline.specs.get(stage)
        if spec is None:
            raise ValueError(f"stage {stage!r} is not in the pipeline")
        units: list[str] = []
        for target in targets:
            if spec.stage.unit == "item" and "/" not in target:
                waiting = [r["unit_id"] for r in self.db.query(
                    "SELECT unit_id FROM gate WHERE stage=? AND"
                    " decision='pending' AND unit_id LIKE ?",
                    (stage, f"{target}/%"))]
                units += waiting or [r["id"] for r in self.db.query(
                    "SELECT id FROM item WHERE source=? AND"
                    " lifecycle != 'gone' ORDER BY id", (target,))]
            else:
                units.append(target)
        for uid in units:
            self.db.execute(
                "INSERT INTO gate VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(unit_id, stage) DO UPDATE SET"
                " decision=excluded.decision, by=excluded.by,"
                " note=excluded.note, ts=excluded.ts",
                (uid, stage, decision, by, note, now()),
            )
            self.db.event(f"gate_{decision}", unit=uid, stage=stage,
                          note=note, by=by)
        return units

    def retry(self, stage: str, units: list[str], by: str) -> None:
        """Force `stage` to re-run for these units on the next run."""
        if stage not in self.pipeline.specs:
            raise ValueError(f"stage {stage!r} is not in the pipeline")
        for uid in units:
            self.db.event("retry", unit=uid, stage=stage, by=by)

    def control(self, action: str, by: str,
                run_id: int | None = None) -> int:
        """pause, resume or cancel a run (the latest running one by
        default). Returns the run id."""
        if action not in ("pause", "resume", "cancel"):
            raise ValueError(f"bad action {action!r}")
        self.reap_stale_runs()
        run = self.db.one(
            "SELECT id FROM run WHERE id=?" if run_id
            else "SELECT id FROM run WHERE status='running'"
            " ORDER BY id DESC LIMIT 1",
            (run_id,) if run_id else (),
        )
        if run is None:
            raise LookupError("no running run")
        self.db.execute("UPDATE run SET control=? WHERE id=?",
                        (None if action == "resume" else action, run["id"]))
        self.db.event(f"run_{action}", run_id=run["id"], by=by)
        return run["id"]

    def kill_agent(self, attempt_dir: Path, by: str) -> int:
        """Kill a runaway agent by the pid its backend recorded. The
        stage then records an error for that unit. Returns the pid."""
        attempt_dir = attempt_dir.resolve()
        if not attempt_dir.is_relative_to(self.config.work_dir.resolve()):
            raise ValueError("not an attempt directory")
        pid = running_pid(attempt_dir)
        if pid is None:
            raise LookupError("agent is not running")
        # Backends start agents in their own session: kill the group.
        os.killpg(pid, signal.SIGKILL)
        self.db.event("agent_killed", by=by, pid=pid,
                      attempt_dir=str(attempt_dir))
        return pid


STALE_AFTER = timedelta(minutes=15)


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
