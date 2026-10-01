"""Wiring shared by the CLI and the web UI."""

from __future__ import annotations

import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from .agents import make_backend
from .agents.base import running_pid
from .builder.pool import BuilderPool, make_pool
from .config import Config, load_config
from .core.context import Paths, Units
from .core.pipeline import Pipeline
from .core.pipeline import load as load_pipeline
from .core.scheduler import Cancelled, Scheduler
from .core.stage import Kind, discover
from .db import DB, PENDING_GATES, now
from .filters import Filter, all_items, select
from .ingest import fetch, parse, read_html
from .inventory import Diff, save_snapshot, store


class App:
    def __init__(self, root: Path):
        self.config: Config = load_config(root)
        self.db = DB(self.config.db_path)
        self._pipeline: Pipeline | None = None
        self._builders: BuilderPool | None = None

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
        return {n: make_backend(n, {"state_dir": self.config.state_dir,
                                    **self.config.backends.get(n, {})})
                for n in names}

    @property
    def builders(self) -> BuilderPool:
        if self._builders is None:
            self._builders = make_pool(
                self.config.builders,
                self.config.concurrency.get(Kind.BUILD, 4),
                self.config.state_dir)
        return self._builders

    def lxd_builders(self) -> list:
        from .builder.lxd import LxdBuilder

        return [b for b in self.builders.builders
                if isinstance(b, LxdBuilder)]

    def build_builder_image(self, log=print) -> str:
        """Build the LXD builder image on the first LXD builder's host
        for the latest snapshot's series, and copy it to the others.
        Workers pick it up (are recreated) at their next build."""
        from .builder.lxd import build_image

        lxds = self._lxd_builders_or_fail()
        snap = self.db.one(
            "SELECT series FROM snapshot ORDER BY id DESC LIMIT 1")
        if snap is None:
            raise ValueError("no snapshot yet: run `ftbfs ingest` first")
        arches = sorted({a for b in lxds for a in b.arches})
        first = lxds[0]
        build_image(first.lxd, snap["series"], arches, first.image,
                    log=log)
        return self.sync_builder_image(log)

    def sync_builder_image(self, log=print) -> str:
        """Copy the first LXD builder host's image to the other hosts
        (e.g. after adding one)."""
        from .builder.lxd import LxdError, copy_image

        lxds = self._lxd_builders_or_fail()
        first = lxds[0]
        fp = first.lxd.image(first.image)
        if fp is None:
            raise LxdError(f"{first.remote}: no {first.image} image; run"
                           " `ftbfs builders image`")
        for b in {b.remote: b for b in lxds}.values():
            if b.remote != first.remote:
                log(f"{b.remote}: image {fp[:12]}")
                copy_image(first.lxd, b.lxd, first.image)
        return fp

    def _lxd_builders_or_fail(self) -> list:
        lxds = self.lxd_builders()
        if not lxds:
            raise ValueError("no LXD builders in config.local.toml")
        return lxds

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

    def decided(self) -> dict[tuple[str, str], str]:
        """(source, version) -> disposition status."""
        return {(r["source"], r["version"]): r["status"]
                for r in self.db.query("SELECT * FROM disposition")}

    def workable(self, flt: Filter) -> list:
        """The selection minus source versions a human has decided on
        (accepted, uploaded, ...): runs stop spending on them."""
        decided = self.decided()
        return [r for r in self.select(flt)
                if (r["source"], r["version"]) not in decided]

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
            self.config.concurrency, run_id, self.builders,
            self.config.identity,
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
        items = self.workable(flt)
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

    def plan(self, flt: Filter, until: str | None = None
             ) -> dict[str, dict[str, list[str]]]:
        """Dry run: per stage, the units a run with this selection
        would run, find cached, hold at a gate, ... (Scheduler.plan)."""
        sched = self.scheduler(Units(self.workable(flt), self.db))
        return sched.plan(sched.stage_names(until=until))

    def start_run(self, until: str | None = None, ingest: bool = False,
                  by: str = "web", wait_s: float = 10.0) -> int:
        """Start `ftbfs run` on the configured selection as a detached
        process, so it outlives the caller (e.g. a UI restart). Returns
        its run id once the run is recorded."""
        self.reap_stale_runs()
        if self.db.one("SELECT 1 FROM run WHERE status='running'"):
            raise ValueError("a run is already in progress")
        if until is not None and until not in self.pipeline.specs:
            raise ValueError(f"stage {until!r} is not in the pipeline")
        args = [sys.executable, "-m", "ftbfs", "--root",
                str(self.config.root), "run", "--trigger", by]
        if until:
            args += ["--until", until]
        if ingest:
            args.append("--ingest")
        logs = self.config.state_dir / "runs"
        logs.mkdir(parents=True, exist_ok=True)
        stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%S")
        with open(logs / f"{stamp}.log", "ab") as log:
            proc = subprocess.Popen(
                args, stdin=subprocess.DEVNULL, stdout=log,
                stderr=subprocess.STDOUT, start_new_session=True,
                cwd=self.config.root)
        self.db.event("run_requested", by=by, pid=proc.pid, until=until,
                      ingest=ingest, log=str(logs / f"{stamp}.log"))
        deadline = time.monotonic() + wait_s
        while time.monotonic() < deadline:
            row = self.db.one("SELECT id FROM run WHERE pid=? AND host=?"
                              " ORDER BY id DESC LIMIT 1",
                              (proc.pid, socket.gethostname()))
            if row:
                return row["id"]
            if proc.poll() is not None:
                raise RuntimeError(f"run exited with {proc.returncode};"
                                   f" see {log.name}")
            time.sleep(0.2)
        raise RuntimeError(f"run did not start within {wait_s:.0f}s;"
                           f" see {log.name}")

    def explain(self, item_ids: list[str], flt: Filter) -> dict:
        """Per item: filter reasons, then per-stage decisions."""
        rows = {r["id"]: r for r in all_items(self.db)}
        out = {}
        decided = self.decided()
        selected = self.workable(flt)
        units = Units(selected, self.db)
        sched = self.scheduler(units)
        for iid in item_ids:
            row = rows[iid]
            reasons = flt.explain(row)
            status = decided.get((row["source"], row["version"]))
            if status:
                reasons.append(f"you decided: {status} (clear it on the"
                               " package page to resume)")
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
                    f"SELECT unit_id FROM ({PENDING_GATES}) WHERE"
                    " stage=? AND unit_id LIKE ?",
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

    def dispose(self, source: str, version: str, status: str | None,
                by: str, note: str | None = None) -> None:
        """Record what a human did about a source version, outside the
        pipeline. None clears it."""
        if status is not None and status not in DISPOSITIONS:
            raise ValueError(f"bad disposition {status!r}")
        if status is None:
            self.db.execute("DELETE FROM disposition WHERE source=? AND"
                            " version=?", (source, version))
        else:
            self.db.execute(
                "INSERT INTO disposition VALUES (?, ?, ?, ?, ?, ?)"
                " ON CONFLICT(source, version) DO UPDATE SET"
                " status=excluded.status, by=excluded.by,"
                " note=excluded.note, ts=excluded.ts",
                (source, version, status, by, note, now()))
        self.db.event("disposition", unit=f"{source}/{version}",
                      status=status, by=by, note=note)

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

# What a human did about a source version. Any of them takes it out of
# the Next steps inbox, except accepted, which still waits for upload.
DISPOSITIONS = {
    "accepted": "fix reviewed and accepted, to upload",
    "uploaded": "uploaded or sent for sponsorship",
    "rejected": "won't fix here",
    "handled": "handled elsewhere (sync, merge, someone else)",
}


def _alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True
