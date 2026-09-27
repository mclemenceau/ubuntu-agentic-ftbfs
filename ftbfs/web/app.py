"""The web UI: a window onto the pipeline database, plus its controls.

A separate process from the runner; both share SQLite in WAL mode, so
the UI can be restarted without disturbing a run. Pages are server
rendered (Jinja); htmx does the forms and partial refreshes, and
Server-Sent Events push new events and agent transcripts live.

Every control (gate decision, retry, pause, kill) goes through the same
App methods as the CLI and is recorded as an event.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from collections import defaultdict
from html import escape
from pathlib import Path
from urllib.parse import quote

from fastapi import FastAPI, Form, HTTPException, Request
from fastapi.responses import (
    HTMLResponse,
    PlainTextResponse,
    RedirectResponse,
    Response,
    StreamingResponse,
)
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from markdown_it import MarkdownIt
from markupsafe import Markup

from ..agents.base import running_pid
from ..app import App
from ..core.stage import UnitType
from ..report import Result, Section
from . import queries as q
from . import transcript

HERE = Path(__file__).parent
TICK_S = 2.0
MAX_FILE = 20_000_000
LOOPBACK = {"127.0.0.1", "localhost", "::1", "[::1]", "testserver"}

_md = MarkdownIt("commonmark", {"html": False}).enable("table")


def markdown(text: str) -> Markup:
    # Raw HTML in the source is escaped (html=False), so LLM-written
    # text cannot inject markup; the rendered result is safe to embed.
    return Markup(_md.render(text or ""))


def ago(seconds: int | None) -> str:
    if seconds is None:
        return "-"
    m, s = divmod(max(0, seconds), 60)
    h, m = divmod(m, 60)
    return f"{h}h {m:02}m" if h else f"{m}m {s:02}s" if m else f"{s}s"


def money(v) -> str:
    return f"${v:.3f}" if v else "-"


def create_app(root: Path, loopback_only: bool = True) -> FastAPI:
    core = App(root)
    work = core.config.work_dir.resolve()
    readable = [work, core.config.cache_dir.resolve(),
                core.config.snapshots_dir.resolve()]
    web = FastAPI(title="ftbfs", docs_url=None, redoc_url=None)
    web.mount("/static", StaticFiles(directory=HERE / "static"),
              name="static")
    tpl = Jinja2Templates(directory=HERE / "templates")
    tpl.env.filters.update(markdown=markdown, ago=ago, money=money,
                           urlq=lambda s: quote(str(s), safe=""),
                           payload=q.payload, fromjson=json.loads)
    tpl.env.globals["unit_types"] = {
        s.name: str(s.stage.unit) for s in core.pipeline}

    @web.middleware("http")
    async def guard(request: Request, call_next):
        # Controls are plain POSTs: require the htmx header, which a
        # cross-site form cannot send, and refuse foreign Host headers
        # (DNS rebinding) when serving on loopback.
        host = (request.headers.get("host") or "").rsplit(":", 1)[0]
        if loopback_only and host not in LOOPBACK:
            return PlainTextResponse("forbidden host", status_code=403)
        if request.method == "POST" and \
                request.headers.get("hx-request") != "true":
            return PlainTextResponse("POST needs HX-Request",
                                     status_code=403)
        return await call_next(request)

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        return tpl.TemplateResponse(request, name, {
            "nav_gates": core.db.one("SELECT COUNT(*) AS n FROM gate WHERE"
                                     " decision='pending'")["n"],
            "nav_attention": len(q.attention(core.db)["problems"]),
            "running": core.db.one("SELECT id FROM run WHERE"
                                   " status='running' ORDER BY id DESC"
                                   " LIMIT 1"),
            **ctx,
        })

    def readable_path(path: str) -> Path:
        p = Path(path)
        p = (p if p.is_absolute() else core.config.root / p).resolve()
        if not any(p.is_relative_to(d) for d in readable):
            raise HTTPException(403, "outside work/, cache/ and snapshots")
        if not p.exists():
            raise HTTPException(404, "no such file")
        return p

    # -- overview ---------------------------------------------------------

    @web.get("/", response_class=HTMLResponse)
    def overview(request: Request):
        core.reap_stale_runs()
        snaps = q.snapshots(core.db)
        diff = (q.snapshot_diff(core.db, snaps[1]["id"], snaps[0]["id"])
                if len(snaps) > 1 else None)
        return page(request, "overview.html",
                    snapshot=snaps[0] if snaps else None, diff=diff,
                    lifecycle=q.lifecycle_counts(core.db),
                    runs=q.runs(core.db, 8),
                    layers=q.dag_layers(core.pipeline),
                    pipeline=core.pipeline,
                    counts=q.stage_counts(core.db),
                    cost=q.total_cost(core.db))

    # -- runs -------------------------------------------------------------

    @web.get("/runs", response_class=HTMLResponse)
    def runs(request: Request):
        core.reap_stale_runs()
        return page(request, "runs.html", runs=q.runs(core.db, 100))

    @web.get("/live")
    def live():
        row = core.db.one("SELECT id FROM run ORDER BY"
                          " status='running' DESC, id DESC LIMIT 1")
        if row is None:
            return RedirectResponse("/runs", status_code=303)
        return RedirectResponse(f"/runs/{row['id']}", status_code=303)

    @web.get("/runs/{run_id}", response_class=HTMLResponse)
    def run_view(request: Request, run_id: int):
        core.reap_stale_runs()
        try:
            state = q.run_state(core.db, run_id)
        except LookupError:
            raise HTTPException(404) from None
        recent = list(reversed(core.db.query(
            "SELECT * FROM event WHERE run_id=? ORDER BY id DESC LIMIT 100",
            (run_id,))))
        return page(request, "run.html", s=state, pipeline=core.pipeline,
                    slots=core.config.concurrency,
                    events=recent,
                    after=recent[-1]["id"] if recent else 0)

    @web.get("/runs/{run_id}/panel", response_class=HTMLResponse)
    def run_panel(request: Request, run_id: int):
        try:
            state = q.run_state(core.db, run_id)
        except LookupError:
            raise HTTPException(404) from None
        return tpl.TemplateResponse(request, "_run_panel.html", {
            "s": state, "pipeline": core.pipeline,
            "slots": core.config.concurrency})

    @web.post("/runs/{run_id}/control", response_class=HTMLResponse)
    def run_control(request: Request, run_id: int,
                    action: str = Form(...)):
        try:
            core.control(action, "web", run_id)
        except (LookupError, ValueError) as e:
            raise HTTPException(400, str(e)) from None
        return run_panel(request, run_id)

    # -- live streams -----------------------------------------------------

    def _sse(event: str, html: str, eid: int | None = None) -> str:
        head = f"id: {eid}\n" if eid is not None else ""
        data = "\n".join(f"data: {line}" for line in html.splitlines())
        return f"{head}event: {event}\n{data or 'data: '}\n\n"

    def _last_id(request: Request, after: int | None) -> int:
        header = request.headers.get("last-event-id")
        if header and header.isdigit():
            return int(header)
        if after is not None:
            return after
        return max(0, q.last_event_id(core.db) - 50)

    @web.get("/sse/events")
    async def sse_events(request: Request, run: int | None = None,
                         unit: str | None = None,
                         stage: str | None = None,
                         after: int | None = None):
        last = _last_id(request, after)
        row_tpl = tpl.env.get_template("_event_row.html")

        async def stream():
            nonlocal last
            while not await request.is_disconnected():
                for e in q.events(core.db, after=last, run_id=run,
                                  unit=unit, stage=stage):
                    last = e["id"]
                    yield _sse("ev", row_tpl.render(e=e), e["id"])
                yield _sse("tick", "")
                await asyncio.sleep(TICK_S)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    def _attempt_dir(path: str) -> Path:
        p = readable_path(path)
        if not p.is_dir() or not p.is_relative_to(work):
            raise HTTPException(400, "not an attempt directory")
        return p

    @web.get("/console", response_class=HTMLResponse)
    def console(request: Request, dir: str):
        adir = _attempt_dir(dir)
        entries, _ = transcript.read(adir / "transcript.jsonl")
        return page(request, "console.html", dir=adir,
                    rel=adir.relative_to(work),
                    alive=running_pid(adir) is not None,
                    files=sorted(p.name for p in adir.iterdir()
                                 if p.is_file()),
                    count=len(entries))

    @web.get("/sse/console")
    async def sse_console(request: Request, dir: str):
        adir = _attempt_dir(dir)
        path = adir / "transcript.jsonl"
        entry_tpl = tpl.env.get_template("_console_entry.html")
        header = request.headers.get("last-event-id")
        offset = 0
        state: dict = {}
        if header and header.isdigit():
            # Rebuild the tool-name map, then resume after what was sent.
            _, offset = transcript.read(path, 0, state)
            offset = min(offset, int(header))

        async def stream():
            nonlocal offset
            while not await request.is_disconnected():
                entries, offset = transcript.read(path, offset, state)
                for en in entries:
                    yield _sse("entry", entry_tpl.render(en=en), offset)
                alive = running_pid(adir) is not None
                yield _sse("tick", "running" if alive else "finished")
                if not alive and not entries:
                    # One last read to catch the final lines, then stop.
                    entries, offset = transcript.read(path, offset, state)
                    for en in entries:
                        yield _sse("entry", entry_tpl.render(en=en), offset)
                    yield _sse("done", "")
                    return
                await asyncio.sleep(1.0)

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

    @web.post("/console/kill", response_class=HTMLResponse)
    def console_kill(dir: str = Form(...)):
        try:
            pid = core.kill_agent(_attempt_dir(dir), "web")
        except (LookupError, ValueError) as e:
            return HTMLResponse(f"<span class=warn>{escape(str(e))}</span>")
        return HTMLResponse(f"<span class=ok>killed pid {pid}</span>")

    # -- items and packages -----------------------------------------------

    def _unit_of(stage: str, item: dict) -> str | None:
        utype = core.pipeline[stage].stage.unit
        if utype == UnitType.ITEM:
            return item["id"]
        if utype == UnitType.PACKAGE:
            return item["source"]
        return item["cluster_id"]

    def _status_matrix(items: list[dict]) -> dict[str, dict[str, str]]:
        ids = {i["id"] for i in items} | {i["source"] for i in items} | {
            i["cluster_id"] for i in items if i["cluster_id"]}
        latest = q.latest_by_unit(core.db, sorted(ids))
        pending = {(g["unit_id"], g["stage"]) for g in q.gates(core.db)}
        out: dict[str, dict[str, str]] = {}
        for i in items:
            row = out.setdefault(i["id"], {})
            for spec in core.pipeline:
                uid = _unit_of(spec.name, i)
                res = latest.get(uid, {}).get(spec.name) if uid else None
                if res is not None:
                    row[spec.name] = res["status"]
                elif (uid, spec.name) in pending:
                    row[spec.name] = "gate"
        return out

    @web.get("/items", response_class=HTMLResponse)
    def items(request: Request, source: str = "", arch: str = "",
              stage: str = "", status: str = "", klass: str = "",
              profile: str = "", everything: bool = False,
              limit: int = 500):
        overrides = {"sources": [source] if source else None,
                     "arches": [arch] if arch else None}
        if everything:
            overrides.update(components=[], states=[], skip_lp_bug=False)
        try:
            flt = core.config.make_filter(profile or None, **overrides)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        rows = [dict(r) for r in core.select(flt)]
        if klass:
            rows = [r for r in rows if r["class"] == klass]
        matrix = _status_matrix(rows)
        if stage:
            # status "" = has any result; "none" = has none yet
            def keep(r):
                st = matrix[r["id"]].get(stage, "none")
                return st == status if status else st != "none"

            rows = [r for r in rows if keep(r)]
        return page(request, "items.html", rows=rows[:limit],
                    total=len(rows), matrix=matrix,
                    stages=list(core.pipeline.specs),
                    args={"source": source, "arch": arch, "stage": stage,
                          "status": status, "klass": klass,
                          "profile": profile, "everything": everything},
                    profiles=sorted(core.config.profiles))

    @web.get("/pkg/{source}", response_class=HTMLResponse)
    def package_latest(source: str):
        versions = core.reporter.versions(source)
        if not versions:
            raise HTTPException(404, f"no package {source}")
        return RedirectResponse(
            f"/pkg/{quote(source)}/{quote(versions[0], safe='')}",
            status_code=303)

    def _attempts(source: str, version: str) -> list[dict]:
        base = work / source / version
        out = []
        for adir in sorted(base.glob("*/*/attempt-*")):
            arch, stage = adir.parent.parent.name, adir.parent.name
            agent = adir / "agent" if (adir / "agent").is_dir() else adir
            files = [p for p in sorted(adir.iterdir()) if p.is_file()]
            if agent is not adir:
                files += [p for p in sorted(agent.iterdir()) if p.is_file()]
            build = adir / "build"
            if build.is_dir():
                files += sorted(build.glob("*.build"))
            usage = {}
            if (agent / "usage.json").exists():
                usage = json.loads((agent / "usage.json").read_text())
            out.append({
                "arch": arch, "stage": stage, "name": adir.name,
                "agent_dir": agent if (agent / "transcript.jsonl").exists()
                else None,
                "files": [f for f in files if f.suffix != ".xz"
                          and f.name != "pid"],
                "usage": usage,
                "n": int(adir.name.split("-")[1]),
            })
        return sorted(out, key=lambda a: (a["arch"], list(
            core.pipeline.specs).index(a["stage"])
            if a["stage"] in core.pipeline.specs else 99, a["n"]))

    @web.get("/pkg/{source}/{version}", response_class=HTMLResponse)
    def package(request: Request, source: str, version: str):
        try:
            inv = core.reporter.gather(source, version)
        except LookupError:
            raise HTTPException(404) from None
        report = core.reporter.render(inv)
        flt = core.config.make_filter()
        why = core.explain([i["id"] for i in inv.items], flt)
        return page(request, "package.html", inv=inv, report=report,
                    why=why, versions=core.reporter.versions(source),
                    attempts=_attempts(source, version),
                    events=q.timeline(core.db, source, version),
                    gated=[s.name for s in core.pipeline if s.gate],
                    stages=list(core.pipeline.specs),
                    matrix=_status_matrix(inv.items))

    @web.get("/pkg/{source}/{version}/investigation.md")
    def package_md(source: str, version: str):
        try:
            text = core.reporter.report(source, version)
        except LookupError:
            raise HTTPException(404) from None
        return PlainTextResponse(text, media_type="text/markdown")

    # -- clusters ---------------------------------------------------------

    @web.get("/clusters", response_class=HTMLResponse)
    def clusters(request: Request, min_size: int = 1, action: str = ""):
        rows = [c for c in q.clusters(core.db) if c["items"] >= min_size]
        if action:
            rows = [c for c in rows
                    if (c["triage"] or {}).get("action") == action]
        return page(request, "clusters.html", rows=rows,
                    args={"min_size": min_size, "action": action})

    @web.get("/cluster", response_class=HTMLResponse)
    def cluster(request: Request, id: str):
        members = [dict(r) for r in core.db.query(
            "SELECT * FROM item WHERE cluster_id=? ORDER BY lifecycle='gone',"
            " source, arch", (id,))]
        if not members:
            raise HTTPException(404)
        sections = []
        for spec in core.pipeline:
            if spec.stage.unit != UnitType.CLUSTER:
                continue
            row = core.db.one(
                "SELECT * FROM stage_result WHERE unit_id=? AND stage=?"
                " ORDER BY id DESC LIMIT 1", (id, spec.name))
            if row is None:
                continue
            s = Section(spec.name, spec.stage.unit, spec.stage.description,
                        [Result(id, row["status"], json.loads(row["data"]),
                                row["attempt"], row["ts"], row["cost"],
                                row["model"], json.loads(row["artifacts"]))])
            env = core.reporter.env
            try:
                t = env.get_template(f"stages/{spec.name}.md.j2")
            except Exception:
                t = env.get_template("stages/_default.md.j2")
            sections.append(t.render(inv=None, s=s))
        return page(request, "cluster.html", id=id, members=members,
                    sections=sections,
                    events=core.db.query(
                        "SELECT * FROM event WHERE unit=? ORDER BY id",
                        (id,)))

    # -- gates, retries, attention ----------------------------------------

    def _gate_context(g) -> dict:
        uid = g["unit_id"]
        item = core.db.one("SELECT * FROM item WHERE id=?", (uid,))
        ctx = {"item": dict(item) if item else None}
        if item and item["cluster_id"]:
            for stage in ("triage", "diagnose"):
                r = core.db.one(
                    "SELECT data FROM stage_result WHERE unit_id=? AND"
                    " stage=? AND status='ok' ORDER BY id DESC LIMIT 1",
                    (item["cluster_id"], stage))
                ctx[stage] = json.loads(r["data"]) if r else None
        if item:
            r = core.db.one(
                "SELECT data FROM stage_result WHERE unit_id=? AND"
                " stage='reproduce' ORDER BY id DESC LIMIT 1", (uid,))
            ctx["reproduce"] = json.loads(r["data"]) if r else None
        return ctx

    @web.get("/gates", response_class=HTMLResponse)
    def gates(request: Request):
        grouped: dict[str, list] = defaultdict(list)
        for g in q.gates(core.db):
            grouped[g["stage"]].append({**dict(g), **_gate_context(g)})
        return page(request, "gates.html", grouped=grouped,
                    recent=core.db.query(
                        "SELECT * FROM gate WHERE decision != 'pending'"
                        " ORDER BY ts DESC LIMIT 30"))

    @web.post("/gates/decide", response_class=HTMLResponse)
    def gate_decide(stage: str = Form(...), unit: str = Form(...),
                    decision: str = Form(...), note: str = Form("")):
        try:
            core.approve(stage, [unit], decision, "web", note or None)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        css = "ok" if decision == "approved" else "fail"
        return HTMLResponse(f"<span class='badge {css}'>{decision}</span>")

    @web.post("/retry", response_class=HTMLResponse)
    def retry(stage: str = Form(...), unit: str = Form(...)):
        try:
            core.retry(stage, [unit], "web")
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return HTMLResponse("<span class='badge pending'>retry requested;"
                            " runs on the next <code>ftbfs run</code>"
                            "</span>")

    @web.get("/attention", response_class=HTMLResponse)
    def attention(request: Request):
        return page(request, "attention.html", a=q.attention(core.db))

    # -- costs, snapshots, files ------------------------------------------

    @web.get("/costs", response_class=HTMLResponse)
    def costs(request: Request):
        return page(request, "costs.html", L=q.ledger(core.db))

    @web.get("/snapshots", response_class=HTMLResponse)
    def snapshots(request: Request, a: int | None = None,
                  b: int | None = None):
        snaps = q.snapshots(core.db)
        diff = None
        if len(snaps) > 1:
            b = b or snaps[0]["id"]
            a = a or next((s["id"] for s in snaps if s["id"] < b),
                          snaps[-1]["id"])
            try:
                diff = q.snapshot_diff(core.db, a, b)
            except LookupError:
                raise HTTPException(404) from None
        return page(request, "snapshots.html", snaps=snaps, diff=diff,
                    a=a, b=b)

    @web.get("/file")
    def file(path: str):
        p = readable_path(path)
        if p.is_dir():
            listing = "\n".join(sorted(
                c.name + ("/" if c.is_dir() else "") for c in p.iterdir()))
            return PlainTextResponse(listing)
        if p.stat().st_size > MAX_FILE * (4 if p.suffix == ".gz" else 1):
            raise HTTPException(413, "file too large to show")
        if p.suffix == ".gz":
            data = gzip.decompress(p.read_bytes())[:MAX_FILE]
        else:
            data = p.read_bytes()[:MAX_FILE]
        media = ("application/json" if p.suffix == ".json" else
                 "text/plain; charset=utf-8")
        return Response(data, media_type=media)

    @web.get("/healthz")
    def healthz():
        return {"ok": True}

    return web

