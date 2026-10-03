# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""The web UI: a window onto the pipeline database, plus its controls.

A separate process from the runner; both share SQLite in WAL mode, so
the UI can be restarted without disturbing a run. Pages are server
rendered (Jinja); htmx does the forms and partial refreshes, and
Server-Sent Events push new events and agent transcripts live.

Every control (gate decision, retry, pause, kill) goes through the same
App methods as the CLI and is recorded as an event, with the login of
who did it. Reading is public, except costs; controls need a role
(`auth.Role`), checked by the `require` dependency each POST route
declares.
"""

from __future__ import annotations

import asyncio
import gzip
import json
from collections import defaultdict
from html import escape
from importlib import metadata
from pathlib import Path
from typing import Annotated
from urllib.parse import quote, urlsplit

from fastapi import Depends, FastAPI, Form, HTTPException, Request
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
from ..app import DISPOSITIONS, App
from ..core.stage import UnitType
from ..db import PENDING_GATES
from ..facts.derive import SIGNALS
from ..report import Result, Section
from . import nextsteps, transcript
from . import queries as q
from .auth import (
    ANONYMOUS,
    LOCAL,
    OAUTH_COOKIE,
    OAUTH_TTL_S,
    SESSION_COOKIE,
    AuthSettings,
    Launchpad,
    LoginError,
    Role,
    Signer,
    User,
    check_serving,
)

HERE = Path(__file__).parent
TICK_S = 2.0
MAX_FILE = 20_000_000
# Files under work/ that record what agents cost: viewers and up only.
COST_FILES = {"usage.json", "transcript.jsonl", "investigation.md"}
# Pages show LLM output, build logs and package metadata: no script
# but our own, nothing loaded from elsewhere, no framing. Styles may be
# inline (style attributes, and htmx adds its indicator style).
HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; style-src 'self' 'unsafe-inline';"
        " img-src 'self' data:; frame-ancestors 'none'; base-uri 'none';"
        " form-action 'self'"),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
}

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


def host_name(header: str) -> str:
    """The name in a Host header, without the port or IPv6 brackets."""
    header = header.strip().lower()
    if header.startswith("["):
        return header[1:].split("]", 1)[0]
    return header.rsplit(":", 1)[0] if header.count(":") == 1 else header


def source_url() -> str | None:
    """The project's home page, for the footer link to the source."""
    try:
        urls = metadata.metadata("ftbfs").get_all("Project-URL") or []
    except metadata.PackageNotFoundError:
        return None
    return next((u.split(",", 1)[1].strip() for u in urls
                 if u.lower().startswith("homepage,")), None)


def require(role: Role):
    """Dependency: the request's user, who must have `role`. Every POST
    route declares one (tests/test_auth.py walks the routes)."""
    def dependency(request: Request) -> User:
        user: User = request.state.user
        if not user.can(role):
            raise HTTPException(403 if user.name else 401,
                                f"needs the {role} role")
        return user

    dependency.role = role
    return dependency


# The user of a request, who must have this role.
Anyone = Annotated[User, Depends(require(Role.ANONYMOUS))]
Reviewer = Annotated[User, Depends(require(Role.REVIEWER))]
Operator = Annotated[User, Depends(require(Role.OPERATOR))]


def local_path(path: str) -> str:
    """`path` if it is a path on this site, else /: no open redirect."""
    if path.startswith("/") and not path.startswith("//") \
            and "\\" not in path:
        return path
    return "/"


def create_app(root: Path, launchpad: Launchpad | None = None) -> FastAPI:
    core = App(root)
    check_serving(core.config)
    auth = AuthSettings.from_config(core.config)
    signer = Signer(auth.secret) if auth else None
    if auth and launchpad is None:
        launchpad = Launchpad(auth.consumer_key)
    web_cfg = core.config.web
    daily_cap = web_cfg.get("daily_cost_cap")
    if daily_cap is not None and (isinstance(daily_cap, bool) or
                                  not isinstance(daily_cap, int | float)):
        raise ValueError("[web] daily_cost_cap must be a number (USD)")
    max_streams = int(web_cfg.get("max_streams", 64))
    streams = {"open": 0}
    allowed = {host_name(h) for h in core.config.allowed_hosts}
    work = core.config.work_dir.resolve()
    readable = [work, core.config.cache_dir.resolve(),
                core.config.snapshots_dir.resolve()]
    web = FastAPI(title="ftbfs", docs_url=None, redoc_url=None)
    web.mount("/static", StaticFiles(directory=HERE / "static"),
              name="static")
    tpl = Jinja2Templates(
        directory=HERE / "templates",
        context_processors=[lambda r: {"user": r.state.user}])
    tpl.env.filters.update(markdown=markdown, ago=ago, money=money,
                           urlq=lambda s: quote(str(s), safe=""),
                           payload=q.payload, fromjson=json.loads)
    tpl.env.globals["unit_types"] = {
        s.name: str(s.stage.unit) for s in core.pipeline}
    tpl.env.globals["signal_help"] = SIGNALS
    tpl.env.globals["dispositions"] = DISPOSITIONS
    tpl.env.globals["auth"] = auth is not None
    tpl.env.globals["cost_files"] = COST_FILES
    tpl.env.globals["source_url"] = source_url()

    def current_user(request: Request) -> User:
        if auth is None:
            return LOCAL
        s = signer.loads("session", request.cookies.get(SESSION_COOKIE))
        if not s:
            return ANONYMOUS
        # The role comes from the config the server runs with, not from
        # the cookie; the teams are those seen at login.
        return User(s["name"], auth.role_of(s["name"], set(s["teams"])))

    @web.middleware("http")
    async def guard(request: Request, call_next):
        # Refuse Host headers outside `[web] allowed_hosts` (DNS
        # rebinding). Controls are POSTs from htmx: require its header,
        # which a cross-site form cannot send, and an Origin naming this
        # very host, so no other site can make a browser post here.
        host = request.headers.get("host") or ""
        if host_name(host) not in allowed:
            return PlainTextResponse("forbidden host", status_code=403)
        if request.method == "POST":
            if request.headers.get("hx-request") != "true":
                return PlainTextResponse("POST needs HX-Request",
                                         status_code=403)
            origin = request.headers.get("origin") or ""
            if urlsplit(origin).netloc.lower() != host.lower():
                return PlainTextResponse("POST from another origin",
                                         status_code=403)
        request.state.user = current_user(request)
        response = await call_next(request)
        response.headers.update(HEADERS)
        return response

    def page(request: Request, name: str, **ctx) -> HTMLResponse:
        return tpl.TemplateResponse(request, name, {
            "nav_gates": core.db.one("SELECT COUNT(*) AS n FROM"
                                     f" ({PENDING_GATES})")["n"],
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

    # -- next steps -------------------------------------------------------

    @web.get("/", response_class=HTMLResponse)
    def next_steps(request: Request):
        core.reap_stale_runs()
        return page(request, "next.html",
                    n=nextsteps.build(core, core.config.make_filter()),
                    pipeline=core.pipeline, until="")

    @web.get("/next/preview", response_class=HTMLResponse)
    def next_preview(request: Request, until: str = ""):
        if until and until not in core.pipeline.specs:
            raise HTTPException(400, f"no stage {until!r}")
        plan = core.plan(core.config.make_filter(), until or None)
        return tpl.TemplateResponse(request, "_run_preview.html", {
            "preview": nextsteps.preview(core, plan), "until": until,
            "pipeline": core.pipeline,
            "running": core.db.one("SELECT id FROM run WHERE"
                                   " status='running' LIMIT 1")})

    @web.post("/runs/start", response_class=HTMLResponse)
    def run_start(user: Operator, until: str = Form(""),
                  ingest: bool = Form(False)):
        try:
            run_id = core.start_run(until or None, ingest, user.by,
                                    daily_cap=daily_cap)
        except (ValueError, RuntimeError) as e:
            return HTMLResponse(f"<span class=warn>{escape(str(e))}</span>")
        return HTMLResponse("", headers={"HX-Redirect": f"/runs/{run_id}"})

    @web.post("/dispose", response_class=HTMLResponse)
    def dispose(user: Reviewer, source: str = Form(...),
                version: str = Form(...), status: str = Form(""),
                note: str = Form("")):
        try:
            core.dispose(source, version, status or None, user.by,
                         note or None)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        if not status:
            return HTMLResponse("<span class='badge'>cleared</span>")
        return HTMLResponse(f"<span class='badge ok'>{escape(status)}"
                            "</span>")

    # -- overview ---------------------------------------------------------

    @web.get("/overview", response_class=HTMLResponse)
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
                    slots=core.builders.slots,
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
            "slots": core.builders.slots})

    @web.post("/runs/{run_id}/control", response_class=HTMLResponse)
    def run_control(user: Operator, request: Request, run_id: int,
                    action: str = Form(...)):
        try:
            core.control(action, user.by, run_id)
        except (LookupError, ValueError) as e:
            raise HTTPException(400, str(e)) from None
        return run_panel(request, run_id)

    # -- live streams -----------------------------------------------------

    def _sse(event: str, html: str, eid: int | None = None) -> str:
        head = f"id: {eid}\n" if eid is not None else ""
        data = "\n".join(f"data: {line}" for line in html.splitlines())
        return f"{head}event: {event}\n{data or 'data: '}\n\n"

    def _bounded(gen):
        """At most `[web] max_streams` streams open at once: each holds
        a connection and polls the database. Over the limit, the stream
        ends at once and asks the browser to retry in a minute. Counted
        inside the generator, so a stream never started is not."""
        async def stream():
            if streams["open"] >= max_streams:
                yield "retry: 60000\n\n"
                return
            streams["open"] += 1
            try:
                async for chunk in gen:
                    yield chunk
            finally:
                streams["open"] -= 1

        return StreamingResponse(stream(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache"})

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
        user = request.state.user

        async def stream():
            nonlocal last
            while not await request.is_disconnected():
                for e in q.events(core.db, after=last, run_id=run,
                                  unit=unit, stage=stage):
                    last = e["id"]
                    yield _sse("ev", row_tpl.render(e=e, user=user),
                               e["id"])
                yield _sse("tick", "")
                await asyncio.sleep(TICK_S)

        return _bounded(stream())

    def _attempt_dir(path: str) -> Path:
        p = readable_path(path)
        if not p.is_dir() or not p.is_relative_to(work):
            raise HTTPException(400, "not an attempt directory")
        return p

    @web.get("/console", response_class=HTMLResponse)
    def console(request: Request, dir: str):
        adir = _attempt_dir(dir)
        entries, _ = transcript.read(adir / "transcript.jsonl",
                                     costs=request.state.user.costs)
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
        costs = request.state.user.costs
        offset = 0
        state: dict = {}
        if header and header.isdigit():
            # Rebuild the tool-name map, then resume after what was sent.
            _, offset = transcript.read(path, 0, state, costs)
            offset = min(offset, int(header))

        async def stream():
            nonlocal offset
            while not await request.is_disconnected():
                entries, offset = transcript.read(path, offset, state,
                                                  costs)
                for en in entries:
                    yield _sse("entry", entry_tpl.render(en=en), offset)
                alive = running_pid(adir) is not None
                yield _sse("tick", "running" if alive else "finished")
                if not alive and not entries:
                    # One last read to catch the final lines, then stop.
                    entries, offset = transcript.read(path, offset, state,
                                                      costs)
                    for en in entries:
                        yield _sse("entry", entry_tpl.render(en=en), offset)
                    yield _sse("done", "")
                    return
                await asyncio.sleep(1.0)

        return _bounded(stream())

    @web.post("/console/kill", response_class=HTMLResponse)
    def console_kill(user: Operator, dir: str = Form(...)):
        try:
            pid = core.kill_agent(_attempt_dir(dir), user.by)
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
              profile: str = "", signal: str = "",
              everything: bool = False, limit: int = 500):
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
        facts = q.facts(core.db, sorted({r["source"] for r in rows}))
        if signal:
            rows = [r for r in rows if signal in
                    facts.get(r["source"], {}).get("signals", [])]
        matrix = _status_matrix(rows)
        if stage:
            # status "" = has any result; "none" = has none yet
            def keep(r):
                st = matrix[r["id"]].get(stage, "none")
                return st == status if status else st != "none"

            rows = [r for r in rows if keep(r)]
        return page(request, "items.html", rows=rows[:limit],
                    total=len(rows), matrix=matrix, facts=facts,
                    stages=list(core.pipeline.specs),
                    args={"source": source, "arch": arch, "stage": stage,
                          "status": status, "klass": klass,
                          "profile": profile, "signal": signal,
                          "everything": everything},
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
        report = core.reporter.render(inv, request.state.user.costs)
        flt = core.config.make_filter()
        why = core.explain([i["id"] for i in inv.items], flt)
        return page(request, "package.html", inv=inv, report=report,
                    facts=q.facts(core.db, [source]).get(source),
                    why=why, versions=core.reporter.versions(source),
                    attempts=_attempts(source, version),
                    events=q.timeline(core.db, source, version),
                    gated=[s.name for s in core.pipeline if s.gate],
                    stages=list(core.pipeline.specs),
                    matrix=_status_matrix(inv.items),
                    disposition=q.dispositions(core.db).get(
                        (source, version)))

    @web.get("/pkg/{source}/{version}/investigation.md")
    def package_md(request: Request, source: str, version: str):
        try:
            text = core.reporter.report(source, version,
                                        request.state.user.costs)
        except LookupError:
            raise HTTPException(404) from None
        return PlainTextResponse(text, media_type="text/markdown")

    # -- signals ----------------------------------------------------------

    @web.get("/signals", response_class=HTMLResponse)
    def signals(request: Request, signal: str = "", profile: str = "",
                everything: bool = False):
        overrides = ({"components": [], "states": [], "skip_lp_bug": False}
                     if everything else {})
        try:
            flt = core.config.make_filter(profile or None, **overrides)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        sources = sorted({r["source"] for r in core.select(flt)})
        facts = q.facts(core.db, sources)
        pkgs = [f for s, f in sorted(facts.items())
                if signal in f["signals"]] if signal else []
        return page(request, "signals.html", facts=facts, pkgs=pkgs,
                    counts=q.signal_counts(facts), selected=len(sources),
                    args={"signal": signal, "profile": profile,
                          "everything": everything},
                    profiles=sorted(core.config.profiles))

    # -- clusters ---------------------------------------------------------

    @web.get("/clusters", response_class=HTMLResponse)
    def clusters(request: Request, min_size: int = 1, action: str = "",
                 signal: str = ""):
        rows = [c for c in q.clusters(core.db) if c["items"] >= min_size]
        if action:
            rows = [c for c in rows
                    if (c["triage"] or {}).get("action") == action]
        facts = q.facts(core.db)
        for c in rows:
            c["signals"] = q.signal_counts({
                s: facts[s] for s in c["sources"].split(",") if s in facts})
        if signal:
            rows = [c for c in rows if signal in c["signals"]]
        return page(request, "clusters.html", rows=rows,
                    args={"min_size": min_size, "action": action,
                          "signal": signal})

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
    def gate_decide(user: Reviewer, stage: str = Form(...),
                    unit: str = Form(...), decision: str = Form(...),
                    note: str = Form("")):
        try:
            core.approve(stage, [unit], decision, user.by, note or None)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        css = "ok" if decision == "approved" else "fail"
        return HTMLResponse(f"<span class='badge {css}'>{decision}</span>")

    @web.post("/gates/bulk", response_class=HTMLResponse)
    def gate_bulk(user: Reviewer, stage: str = Form(...),
                  units: str = Form(...), decision: str = Form(...)):
        """Decide a whole bucket. Units need not be at the gate yet: a
        decision recorded ahead is honoured when a run reaches it."""
        try:
            ids = json.loads(units)
            if not isinstance(ids, list) or not all(
                    isinstance(u, str) and u for u in ids):
                raise ValueError("units must be a list of unit ids")
            done = core.approve(stage, ids, decision, user.by)
        except (ValueError, json.JSONDecodeError) as e:
            raise HTTPException(400, str(e)) from None
        css = "ok" if decision == "approved" else "fail"
        return HTMLResponse(f"<span class='badge {css}'>{decision}"
                            f" {len(done)}</span>")

    @web.post("/retry", response_class=HTMLResponse)
    def retry(user: Reviewer, stage: str = Form(...),
              unit: str = Form(...)):
        try:
            core.retry(stage, [unit], user.by)
        except ValueError as e:
            raise HTTPException(400, str(e)) from None
        return HTMLResponse("<span class='badge pending'>retry requested;"
                            " runs on the next <code>ftbfs run</code>"
                            "</span>")

    @web.get("/attention", response_class=HTMLResponse)
    def attention(request: Request):
        return page(request, "attention.html", a=q.attention(core.db))

    # -- costs, snapshots, files ------------------------------------------

    @web.get("/costs", response_class=HTMLResponse,
             dependencies=[Depends(require(Role.VIEWER))])
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
    def file(request: Request, path: str):
        p = readable_path(path)
        if p.name in COST_FILES and not request.state.user.costs:
            raise HTTPException(403, "this file records costs: log in")
        if p.is_dir():
            listing = "\n".join(sorted(
                c.name + ("/" if c.is_dir() else "") for c in p.iterdir()))
            return PlainTextResponse(listing)
        if p.stat().st_size > MAX_FILE * (4 if p.suffix == ".gz" else 1):
            raise HTTPException(413, "file too large to show")
        # Read at most MAX_FILE bytes, also when decompressing: a small
        # .gz can expand to gigabytes.
        try:
            with (gzip.open(p) if p.suffix == ".gz" else p.open("rb")) as f:
                data = f.read(MAX_FILE)
        except (OSError, EOFError) as e:
            raise HTTPException(422, f"cannot read: {e}") from None
        media = ("application/json" if p.suffix == ".json" else
                 "text/plain; charset=utf-8")
        return Response(data, media_type=media)

    @web.get("/healthz")
    def healthz():
        return {"ok": True}

    # -- login ------------------------------------------------------------

    def _cookie(response: Response, name: str, value: str,
                ttl_s: float) -> None:
        response.set_cookie(name, value, max_age=int(ttl_s), path="/",
                            httponly=True, samesite="lax",
                            secure=auth.secure)

    def _message(request: Request, title: str, text: str,
                 status: int = 200) -> HTMLResponse:
        # `text` is ours, never user input: it may carry a link.
        r = page(request, "message.html", title=title, text=Markup(text))
        r.status_code = status
        return r

    @web.get("/login")
    def login(next: str = "/"):
        """Off to Launchpad, with the request token's secret kept in a
        signed cookie of this browser until it comes back."""
        if auth is None:
            raise HTTPException(404, "no login: [web.auth] is not set")
        try:
            token, secret = launchpad.request_token()
        except LoginError as e:
            raise HTTPException(502, str(e)) from None
        response = RedirectResponse(launchpad.authorize_url(
            token, f"{auth.public_url}/auth/callback"), status_code=303)
        _cookie(response, OAUTH_COOKIE, signer.dumps("oauth", {
            "token": token, "secret": secret,
            "next": local_path(next)}, OAUTH_TTL_S), OAUTH_TTL_S)
        return response

    @web.get("/auth/callback")
    def auth_callback(request: Request, oauth_token: str = ""):
        if auth is None:
            raise HTTPException(404)
        pending = signer.loads("oauth", request.cookies.get(OAUTH_COOKIE))
        if not pending or (oauth_token and
                           oauth_token != pending["token"]):
            return _message(request, "Login expired",
                            "This login was started elsewhere or too long"
                            " ago. <a href='/login'>Log in again</a>.",
                            400)
        try:
            token, secret = launchpad.access_token(pending["token"],
                                                   pending["secret"])
        except LoginError:
            return _message(request, "Not logged in",
                            "Launchpad did not grant access, so you are"
                            " not logged in. <a href='/login'>Try"
                            " again</a>.", 403)
        try:
            name = launchpad.whoami(token, secret)
            teams = (launchpad.teams(name) & auth.names
                     if auth.names - {name} else set())
        except LoginError as e:
            raise HTTPException(502, str(e)) from None
        # The token only proved who this is: it is not kept.
        response = RedirectResponse(pending["next"], status_code=303)
        ttl = auth.session_days * 86400
        _cookie(response, SESSION_COOKIE, signer.dumps(
            "session", {"name": name, "teams": sorted(teams)}, ttl), ttl)
        response.delete_cookie(OAUTH_COOKIE, path="/")
        return response

    @web.post("/logout")
    def logout(user: Anyone):
        response = Response(headers={"HX-Redirect": "/"})
        response.delete_cookie(SESSION_COOKIE, path="/")
        return response

    return web

