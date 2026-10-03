# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Web login, roles, attribution and cost hiding, with a fake
Launchpad."""

import json
import re
import shutil
from pathlib import Path
from urllib.parse import parse_qs, quote, urlsplit

import pytest
from fastapi.routing import APIRoute
from fastapi.testclient import TestClient
from starlette.requests import Request

from ftbfs.app import App
from ftbfs.config import load_config
from ftbfs.db import now
from ftbfs.ingest import parse, read_html
from ftbfs.inventory import save_snapshot, store
from ftbfs.web import auth as auth_mod
from ftbfs.web.app import create_app
from ftbfs.web.auth import (
    OAUTH_COOKIE,
    SESSION_COOKIE,
    AuthSettings,
    Launchpad,
    LoginError,
    Role,
    Signer,
    check_serving,
)

ROOT = Path(__file__).parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "ftbfs-2026-09-27.html.gz"
BASE = "http://127.0.0.1:8047"
HX = {"HX-Request": "true", "Origin": BASE}
SECRET = b"s" * 40
# A cost no page may show to an anonymous visitor, in any format.
SENTINEL = 7.654321
LEAKS = re.compile(r"7\.65|7\.654|7654|\b4242\b")  # cost, tokens


def configure(root: Path, extra: str = "") -> None:
    secret = root / "session.key"
    secret.write_bytes(SECRET)
    secret.chmod(0o600)
    (root / "config.local.toml").write_text(f"""
[web]
public_url = "{BASE}"
session_secret_file = "{secret}"
daily_cost_cap = 5.0
{extra}
[web.auth]
provider = "launchpad"

[web.roles]
viewer = ["~viewers"]
reviewer = ["rita"]
operator = ["olga"]
""")


class FakeLaunchpad:
    """Launchpad's side of the login: who approves, and their teams."""

    def __init__(self):
        self.person = "rita"
        self.declined = False
        self.team_of = {"vera": {"viewers", "other-team"},
                        "rita": {"other-team"}}
        self.team_calls = 0

    def request_token(self):
        return "rt", "rs"

    def authorize_url(self, token, callback):
        return f"https://lp.example/+authorize-token?oauth_token={token}"

    def access_token(self, token, secret):
        if self.declined or (token, secret) != ("rt", "rs"):
            raise LoginError("declined")
        return "at", "as"

    def whoami(self, token, secret):
        assert (token, secret) == ("at", "as")
        return self.person

    def teams(self, name):
        self.team_calls += 1
        return self.team_of.get(name, set())


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    root = tmp_path_factory.mktemp("authproj")
    shutil.copy(ROOT / "pipeline.toml", root)
    shutil.copy(ROOT / "config.toml", root)
    configure(root)
    app = App(root)
    snap = parse(read_html(FIXTURE), fetched_at="2026-09-27T00:00:00+00:00")
    store(app.db, snap, save_snapshot(snap, app.config.snapshots_dir))
    item = app.db.one("SELECT * FROM item WHERE arch='amd64' ORDER BY id")
    iid, src, ver = item["id"], item["source"], item["version"]
    app.db.execute("UPDATE item SET cluster_id='c1', class='k' WHERE id=?",
                   (iid,))
    run_id = app.db.execute(
        "INSERT INTO run (started, finished, status, filter, pipeline_hash,"
        " trigger) VALUES (?, ?, 'done', '{}', 'h', 'web:olga')",
        (now(), now())).lastrowid
    adir = app.config.work_dir / src / ver / "amd64" / "dev" / "attempt-1"
    agent = adir / "agent"
    agent.mkdir(parents=True)
    usage = {"model": "m", "cost": SENTINEL,
             "usage": {"input_tokens": 4242}}
    (agent / "usage.json").write_text(json.dumps(usage))
    (agent / "transcript.jsonl").write_text("".join(json.dumps(x) + "\n"
                                                    for x in [
        {"type": "ftbfs_start", "model": "m", "cwd": str(adir)},
        {"type": "text", "part": {"text": "editing"}},
        {"type": "ftbfs_end", "ok": True, "steps": 3, "cost": SENTINEL},
        {"type": "result", "num_turns": 2, "total_cost_usd": SENTINEL},
        {"type": "mystery", "cost": SENTINEL, "tokens": 4242},
    ]))
    app.db.execute(
        "INSERT INTO stage_result (run_id, unit_type, unit_id, stage,"
        " stage_version, inputs_hash, attempt, status, data, artifacts,"
        " backend, model, usage, cost, ts) VALUES (?, 'item', ?, 'dev',"
        " '1', 'h', 1, 'ok', ?, '[]', 'fake', 'm', ?, ?, ?)",
        (run_id, iid, json.dumps({"summary": "Fix it.", "version": ver}),
         json.dumps(usage["usage"]), SENTINEL, now()))
    app.db.event("agent_call_start", run_id=run_id, stage="dev", unit=iid,
                 model="m", tier="medium", attempt_dir=str(agent))
    app.db.event("agent_call_end", run_id=run_id, stage="dev", unit=iid,
                 ok=True, cost=SENTINEL, usage=usage["usage"])
    app.db.event("unit_end", run_id=run_id, stage="dev", unit=iid,
                 status="ok", cost=SENTINEL)
    app.db.execute("INSERT INTO gate VALUES (?, 'dev', 'pending',"
                   " NULL, NULL, ?)", (iid, now()))
    app.reporter.write(app.config.work_dir, src, ver)
    lp = FakeLaunchpad()
    return {"app": app, "root": root, "iid": iid, "src": src, "ver": ver,
            "run_id": run_id, "agent": agent, "lp": lp,
            "web": create_app(root, launchpad=lp)}


def client(project, person: str | None = None) -> TestClient:
    """A browser, logged in through the whole flow as `person`."""
    c = TestClient(project["web"], base_url=BASE)
    if person:
        project["lp"].person = person
        r = c.get("/login?next=/gates", follow_redirects=False)
        assert r.status_code == 303
        assert r.headers["location"].startswith("https://lp.example/")
        r = c.get("/auth/callback?oauth_token=rt", follow_redirects=False)
        assert r.status_code == 303 and r.headers["location"] == "/gates"
        assert c.cookies.get(SESSION_COOKIE)
        assert not c.cookies.get(OAUTH_COOKIE)  # used once
    return c


# -- the rule every route follows -----------------------------------------

def test_every_post_route_declares_a_role(project):
    posts = [r for r in project["web"].routes
             if isinstance(r, APIRoute) and "POST" in r.methods]
    assert len(posts) >= 8
    for route in posts:
        roles = [d.call.role for d in route.dependant.dependencies
                 if hasattr(d.call, "role")]
        assert roles, f"POST {route.path} declares no role"


# -- each control route, for each kind of person --------------------------

CONTROLS = [  # path, form, role needed; forms that change nothing
    ("/runs/start", {"until": "nope"}, Role.OPERATOR),
    ("/runs/1/control", {"action": "bogus"}, Role.OPERATOR),
    ("/console/kill", {"dir": "work"}, Role.OPERATOR),  # nothing running
    ("/gates/decide", {"stage": "dev", "unit": "x", "decision": "bogus"},
     Role.REVIEWER),
    ("/gates/bulk", {"stage": "dev", "units": "[]", "decision": "bogus"},
     Role.REVIEWER),
    ("/retry", {"stage": "nope", "unit": "x"}, Role.REVIEWER),
    ("/dispose", {"source": "s", "version": "1", "status": "nope"},
     Role.REVIEWER),
]
PEOPLE = [(None, Role.ANONYMOUS), ("nobody", Role.ANONYMOUS),
          ("vera", Role.VIEWER), ("rita", Role.REVIEWER),
          ("olga", Role.OPERATOR)]


@pytest.mark.parametrize("person,role", PEOPLE)
def test_control_routes_by_role(project, person, role):
    c = client(project, person)
    for path, form, needed in CONTROLS:
        r = c.post(path, headers=HX, data=form)
        if role >= needed:
            assert r.status_code not in (401, 403), (person, path)
        else:
            assert r.status_code == (403 if person else 401), (person, path)


def test_teams_grant_roles_and_only_listed_teams_are_kept(project):
    lp = project["lp"]
    c = client(project, "vera")
    signer = Signer(SECRET)
    session = signer.loads("session", c.cookies.get(SESSION_COOKIE))
    assert session == {"name": "vera", "teams": ["viewers"]}
    assert c.get("/costs").status_code == 200
    assert "vera &middot; viewer" in c.get("/").text
    # A person not in any role still logs in, and reads as anonymous.
    before = lp.team_calls
    c = client(project, "nobody")
    assert lp.team_calls == before + 1
    assert c.get("/costs").status_code == 403


def test_actions_are_recorded_with_the_login(project):
    app, iid = project["app"], project["iid"]
    c = client(project, "rita")
    r = c.post("/gates/decide", headers=HX, data={
        "stage": "dev", "unit": iid, "decision": "approved"})
    assert r.status_code == 200
    gate = app.db.one("SELECT * FROM gate WHERE unit_id=? AND"
                      " stage='dev'", (iid,))
    assert gate["by"] == "web:rita"
    ev = app.db.one("SELECT payload FROM event WHERE type='gate_approved'"
                    " AND unit=? ORDER BY id DESC", (iid,))
    assert json.loads(ev["payload"])["by"] == "web:rita"
    assert "web:rita" in c.get("/gates").text
    pkg = client(project).get(f"/pkg/{project['src']}/{project['ver']}")
    assert "by=web:rita" in pkg.text  # on the public timeline


def test_daily_cap_stops_web_runs(project):
    app = project["app"]
    # the fixture's web run spent the sentinel today; CLI runs do not count
    assert app.web_spend_today() == pytest.approx(SENTINEL)
    r = client(project, "olga").post("/runs/start", headers=HX,
                                      data={"until": "excerpt"})
    assert "daily cap reached" in r.text
    with pytest.raises(ValueError, match="daily cap"):
        app.start_run("excerpt", daily_cap=5.0)


# -- sessions ---------------------------------------------------------------

def test_tampered_expired_and_foreign_cookies_are_anonymous(project):
    signer = Signer(SECRET)
    good = signer.dumps("session", {"name": "olga", "teams": []}, 60)
    payload, sig = good.split(".")
    forged = signer.dumps("session", {"name": "rita", "teams": []}, 60)
    cookies = {
        "tampered": forged.split(".")[0] + "." + sig,
        "expired": signer.dumps("session", {"name": "olga", "teams": []},
                                -1),
        "other purpose": signer.dumps("oauth", {"name": "olga",
                                                "teams": []}, 60),
        "other secret": Signer(b"x" * 40).dumps(
            "session", {"name": "olga", "teams": []}, 60),
        "garbage": "not.a.cookie.at.all",
    }
    for what, value in cookies.items():
        c = TestClient(project["web"], base_url=BASE,
                       cookies={SESSION_COOKIE: value})
        r = c.post("/retry", headers=HX, data={"stage": "nope",
                                               "unit": "x"})
        assert r.status_code == 401, what
    c = TestClient(project["web"], base_url=BASE,
                   cookies={SESSION_COOKIE: good})
    assert c.post("/retry", headers=HX, data={"stage": "nope", "unit": "x"}
                  ).status_code == 400  # olga: allowed, bad stage


def test_session_cookie_flags(project):
    c = TestClient(project["web"], base_url=BASE)
    r = c.get("/login", follow_redirects=False)
    assert "httponly" in r.headers["set-cookie"].lower()
    assert "samesite=lax" in r.headers["set-cookie"].lower()
    assert "secure" not in r.headers["set-cookie"].lower()  # http here
    r = c.get("/auth/callback", follow_redirects=False)
    cookie = r.headers["set-cookie"].lower()
    assert SESSION_COOKIE in cookie and "max-age=2592000" in cookie


def test_logout(project):
    c = client(project, "rita")
    assert c.post("/logout", headers=HX).headers["hx-redirect"] == "/"
    assert not c.cookies.get(SESSION_COOKIE)
    assert c.post("/retry", headers=HX, data={"stage": "nope",
                                              "unit": "x"}
                  ).status_code == 401


def test_login_failures(project):
    lp = project["lp"]
    c = TestClient(project["web"], base_url=BASE)
    # back from Launchpad without having started here
    r = c.get("/auth/callback?oauth_token=rt", follow_redirects=False)
    assert r.status_code == 400 and not c.cookies.get(SESSION_COOKIE)
    # a token other than the one this browser asked for
    c.get("/login", follow_redirects=False)
    r = c.get("/auth/callback?oauth_token=other", follow_redirects=False)
    assert r.status_code == 400 and not c.cookies.get(SESSION_COOKIE)
    # "No Access" on Launchpad
    lp.declined = True
    try:
        c.get("/login", follow_redirects=False)
        r = c.get("/auth/callback?oauth_token=rt", follow_redirects=False)
        assert r.status_code == 403 and "not logged in" in r.text
        assert not c.cookies.get(SESSION_COOKIE)
    finally:
        lp.declined = False
    # next= only leads back into this site
    for bad in ("//evil.example/x", "https://evil.example", "/\\evil"):
        c.get(f"/login?next={quote(bad)}", follow_redirects=False)
        r = c.get("/auth/callback", follow_redirects=False)
        assert r.headers["location"] == "/", bad


def test_foreign_host_and_origin_with_a_session(project):
    c = client(project, "olga")
    r = c.post("/retry", data={"stage": "nope", "unit": "x"},
               headers={"HX-Request": "true",
                        "Origin": "http://evil.example"})
    assert r.status_code == 403 and "origin" in r.text
    assert c.get("/", headers={"host": "evil.example"}).status_code == 403


# -- what each role sees ------------------------------------------------------

def test_buttons_follow_the_role(project):
    src, ver = project["src"], project["ver"]
    project["app"].db.execute(
        "UPDATE gate SET decision='pending' WHERE unit_id=?",
        (project["iid"],))
    pages = ["/", "/gates", f"/pkg/{src}/{ver}"]
    anon = client(project)
    for url in pages:
        body = anon.get(url).text
        assert "hx-post=\"/gates" not in body and "/dispose" not in body
        assert "log in with Launchpad" in body
    assert 'href="/costs"' not in anon.get("/").text
    rita = client(project, "rita")
    gates = rita.get("/gates").text
    assert 'hx-post="/gates/decide"' in gates
    assert 'hx-post="/runs/start"' not in rita.get("/").text
    assert 'hx-post="/runs/start"' in client(project, "olga").get("/").text


def _get_routes(project) -> list[str]:
    """Every GET page, with real values for its parameters."""
    src, ver, run = project["src"], project["ver"], project["run_id"]
    agent = quote(str(project["agent"]))
    urls = {"/": "/", "/next/preview": "/next/preview",
            "/overview": "/overview", "/runs": "/runs",
            "/runs/{run_id}": f"/runs/{run}",
            "/runs/{run_id}/panel": f"/runs/{run}/panel",
            "/console": f"/console?dir={agent}",
            "/items": "/items?everything=1",
            "/pkg/{source}": f"/pkg/{src}",
            "/pkg/{source}/{version}": f"/pkg/{src}/{ver}",
            "/pkg/{source}/{version}/investigation.md":
                f"/pkg/{src}/{ver}/investigation.md",
            "/signals": "/signals", "/clusters": "/clusters",
            "/cluster": "/cluster?id=c1", "/gates": "/gates",
            "/attention": "/attention", "/costs": "/costs",
            "/snapshots": "/snapshots", "/healthz": "/healthz",
            "/live": "/live", "/login": None, "/auth/callback": None,
            "/file": None, "/sse/events": None, "/sse/console": None}
    gets = {r.path for r in project["web"].routes
            if isinstance(r, APIRoute) and "GET" in r.methods}
    assert gets <= set(urls), f"new GET routes to cover: {gets - set(urls)}"
    return [u for u in urls.values() if u]


@pytest.fixture
def one_tick(monkeypatch):
    """SSE streams run once, then see the client gone."""
    calls = {"n": 0}

    async def disconnected(self):
        calls["n"] += 1
        return calls["n"] > 1

    monkeypatch.setattr(Request, "is_disconnected", disconnected)
    return calls


def test_costs_never_reach_anonymous_visitors(project, one_tick):
    anon = client(project)
    seen = 0
    for url in _get_routes(project):
        r = anon.get(url)
        if url == "/costs":
            assert r.status_code == 401
            continue
        assert r.status_code == 200, url
        assert not LEAKS.search(r.text), url
        seen += 1
    assert seen > 15
    # raw files: those that record costs are refused, the rest served
    work = project["app"].config.work_dir
    for p in sorted(work.rglob("*")):
        r = anon.get(f"/file?path={quote(str(p))}")
        if p.name in ("usage.json", "transcript.jsonl", "investigation.md"):
            assert r.status_code == 403, p
        else:
            assert r.status_code == 200 and not LEAKS.search(r.text), p
    # live streams
    for url in ("/sse/events?after=0",
                f"/sse/console?dir={quote(str(project['agent']))}"):
        one_tick["n"] = 0
        body = anon.get(url).text
        assert "data:" in body and not LEAKS.search(body), url


def test_viewers_see_costs(project, one_tick):
    vera = client(project, "vera")
    src, ver = project["src"], project["ver"]
    assert "7.65" in vera.get("/costs").text
    assert "Recorded LLM cost" in vera.get(
        f"/pkg/{src}/{ver}/investigation.md").text
    one_tick["n"] = 0
    assert "$7.6543" in vera.get(
        f"/sse/console?dir={quote(str(project['agent']))}").text
    one_tick["n"] = 0
    body = vera.get("/sse/events?after=0").text
    assert "cost=7.65" in body
    usage = project["agent"] / "usage.json"
    assert vera.get(f"/file?path={quote(str(usage))}").status_code == 200


def test_streams_are_bounded(project, one_tick, tmp_path):
    root = tmp_path
    for f in ("pipeline.toml", "config.toml"):
        shutil.copy(ROOT / f, root)
    configure(root, "max_streams = 1\n")
    App(root)
    web = create_app(root, launchpad=FakeLaunchpad())
    c = TestClient(web, base_url=BASE)
    # sequential streams each get their turn: the count goes back down
    for _ in range(3):
        one_tick["n"] = 0
        assert "event: tick" in c.get("/sse/events").text
    configure(root, "max_streams = 0\n")
    c = TestClient(create_app(root, launchpad=FakeLaunchpad()),
                   base_url=BASE)
    assert c.get("/sse/events").text == "retry: 60000\n\n"


# -- configuration ----------------------------------------------------------

def test_auth_settings_validation(tmp_path):
    for f in ("pipeline.toml", "config.toml"):
        shutil.copy(ROOT / f, tmp_path)
    configure(tmp_path)
    cfg = load_config(tmp_path)
    s = AuthSettings.from_config(cfg)
    assert s.roles == {Role.VIEWER: {"viewers"}, Role.REVIEWER: {"rita"},
                       Role.OPERATOR: {"olga"}}
    assert s.role_of("rita", set()) == Role.REVIEWER
    assert s.role_of("rita", {"viewers"}) == Role.REVIEWER
    assert s.role_of("x", {"viewers"}) == Role.VIEWER
    assert not s.secure

    def bad(change, match):
        c = load_config(tmp_path)
        change(c.web)
        with pytest.raises(ValueError, match=match):
            AuthSettings.from_config(c)

    bad(lambda w: w["auth"].update(provider="github"), "provider")
    bad(lambda w: w.update(public_url="ftp://x"), "public_url")
    bad(lambda w: w.update(public_url=f"{BASE}/ui"), "public_url")
    bad(lambda w: w.update(public_url="https://ftbfs.example.org"),
        "allowed_hosts")
    bad(lambda w: w.pop("session_secret_file"), "session_secret_file")
    bad(lambda w: w["roles"].update(admin=["x"]), "not a role")
    bad(lambda w: w["roles"].update(reviewer=["Bad Name"]), "Launchpad")
    bad(lambda w: w["roles"].update(reviewer="rita"), "list")
    key = tmp_path / "session.key"
    key.chmod(0o644)
    bad(lambda w: None, "readable by others")
    key.chmod(0o600)
    key.write_bytes(b"short")
    bad(lambda w: None, "too short")
    bad(lambda w: w.update(session_secret_file=str(tmp_path / "none")),
        "head -c 32 /dev/urandom")


def test_daily_cap_must_be_a_number(tmp_path):
    for f in ("pipeline.toml", "config.toml"):
        shutil.copy(ROOT / f, tmp_path)
    (tmp_path / "config.local.toml").write_text(
        '[web]\ndaily_cost_cap = "lots"\n')
    with pytest.raises(ValueError, match="daily_cost_cap"):
        create_app(tmp_path)


def test_auth_allows_serving_beyond_loopback(tmp_path):
    for f in ("pipeline.toml", "config.toml"):
        shutil.copy(ROOT / f, tmp_path)
    configure(tmp_path, 'allowed_hosts = ["127.0.0.1", "ftbfs.example.org"]')
    check_serving(load_config(tmp_path), "0.0.0.0")
    c = TestClient(create_app(tmp_path, launchpad=FakeLaunchpad()),
                   base_url=BASE)
    for host in ("ftbfs.example.org", "FTBFS.example.org:443"):
        assert c.get("/healthz", headers={"host": host}).status_code == 200
    assert c.get("/healthz", headers={"host": "example.org"}
                 ).status_code == 403


def test_no_login_without_auth(tmp_path):
    for f in ("pipeline.toml", "config.toml"):
        shutil.copy(ROOT / f, tmp_path)
    c = TestClient(create_app(tmp_path), base_url=BASE)
    assert c.get("/login").status_code == 404
    assert "log in" not in c.get("/").text
    assert 'href="/costs"' in c.get("/").text  # local: operator


# -- the Launchpad client -----------------------------------------------------

class FakeResponse:
    def __init__(self, body: bytes):
        self.body = body

    def read(self):
        return self.body

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False


def test_launchpad_oauth_calls(monkeypatch):
    sent = []
    answers = {
        "+request-token": b"oauth_token=rt&oauth_token_secret=rs",
        "+access-token": b"oauth_token=at&oauth_token_secret=as&lp.context"
                         b"=None",
        "people/+me": json.dumps({"name": "rita"}).encode(),
        "super_teams?ws.size=300": json.dumps({
            "entries": [{"name": "a"}],
            "next_collection_link": "https://api.lp/~rita/st2"}).encode(),
        "st2": json.dumps({"entries": [{"name": "b"}]}).encode(),
    }

    def urlopen(req, timeout):
        sent.append(req)
        key = next(k for k in answers if req.full_url.endswith(k))
        return FakeResponse(answers[key])

    monkeypatch.setattr(auth_mod, "urlopen", urlopen)
    lp = Launchpad("ftbfs (test)", root="https://lp", api="https://api.lp")
    assert lp.request_token() == ("rt", "rs")
    form = parse_qs(sent[-1].data.decode())
    assert form["oauth_signature"] == ["&"]
    assert form["oauth_consumer_key"] == ["ftbfs (test)"]
    url = urlsplit(lp.authorize_url("rt", f"{BASE}/auth/callback"))
    assert parse_qs(url.query)["allow_permission"] == ["READ_PUBLIC"]
    assert lp.access_token("rt", "rs") == ("at", "as")
    assert parse_qs(sent[-1].data.decode())["oauth_signature"] == ["&rs"]
    assert lp.whoami("at", "as") == "rita"
    assert 'oauth_signature="&as"' in sent[-1].get_header("Authorization")
    assert lp.teams("rita") == {"a", "b"}
    answers["+access-token"] = b"oauth_problem=token_rejected"
    with pytest.raises(LoginError):
        lp.access_token("rt", "rs")
    answers["people/+me"] = json.dumps({"name": "<b>"}).encode()
    with pytest.raises(LoginError):
        lp.whoami("at", "as")
