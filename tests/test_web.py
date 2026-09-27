"""Report, read models and web UI over a small seeded project."""

import json
import shutil
from pathlib import Path
from urllib.parse import quote

import pytest
from fastapi.testclient import TestClient

from ftbfs.app import App
from ftbfs.db import now
from ftbfs.facts.derive import derive
from ftbfs.ingest import parse, read_html
from ftbfs.inventory import save_snapshot, store
from ftbfs.web import queries as q
from ftbfs.web import transcript
from ftbfs.web.app import create_app

ROOT = Path(__file__).parent.parent
FIXTURE = Path(__file__).parent / "fixtures" / "ftbfs-2026-09-27.html.gz"
HX = {"HX-Request": "true"}


def result(app, unit, stage, status, data, utype="item", run_id=None,
           cost=None):
    return app.db.execute(
        "INSERT INTO stage_result (run_id, unit_type, unit_id, stage,"
        " stage_version, inputs_hash, attempt, status, data, artifacts,"
        " cost, ts) VALUES (?,?,?,?, '1', 'h', 1, ?, ?, '[]', ?, ?)",
        (run_id, utype, unit, stage, status, json.dumps(data), cost,
         now())).lastrowid


@pytest.fixture(scope="module")
def project(tmp_path_factory):
    root = tmp_path_factory.mktemp("proj")
    shutil.copy(ROOT / "pipeline.toml", root)
    shutil.copy(ROOT / "config.toml", root)
    app = App(root)
    snap = parse(read_html(FIXTURE), fetched_at="2026-09-27T00:00:00+00:00")
    store(app.db, snap, save_snapshot(snap, app.config.snapshots_dir))
    item = app.db.one("SELECT * FROM item WHERE arch='amd64' ORDER BY id")
    iid, src, ver = item["id"], item["source"], item["version"]
    app.db.execute("UPDATE item SET cluster_id='c1', class='k' WHERE id=?",
                   (iid,))
    run_id = app.db.execute(
        "INSERT INTO run (started, status, filter, pipeline_hash, trigger)"
        " VALUES (?, 'running', '{}', 'h', 'test')", (now(),)).lastrowid
    result(app, iid, "excerpt", "ok", {
        "text": "boom ``` fence", "key_lines": ["x.c:1: error: a | b"],
        "signature": "s1", "step": "dh_auto_build"}, run_id=run_id)
    result(app, "c1", "triage", "ok", {
        "action": "patch", "fixable": "yes", "summary": "C23 thing",
        "obvious": True, "category": "compile", "confidence": 0.9},
        utype="cluster", cost=0.01)
    result(app, "c1", "diagnose", "ok", {
        "fix_kind": "code-patch", "risk": "low", "confidence": 0.8,
        "root_cause": "<script>alert(1)</script> cause",
        "evidence": ["e1"], "fix_strategy": "fix it",
        "patch_outline": ["step"], "upstream": "none",
        "applies_to_all_members": True}, utype="cluster", cost=0.05)
    facts = derive(src, {ver: ["amd64"]}, None, {"version": "99.0-1"},
                   None, None, [{"id": 1234, "title": "FTBFS: boom",
                                 "done": False, "patch": True,
                                 "severity": "serious", "status": "open",
                                 "url": "https://bugs.debian.org/1234"}])
    facts["upstream"]["homepage"] = "javascript:alert(1)"
    result(app, src, "facts", "ok", facts, utype="package")
    debdiff = root / "fix.debdiff"
    debdiff.write_text("+fixed line\n")
    result(app, iid, "dev", "ok", {
        "summary": "Fix it.", "version": ver + "ubuntu1",
        "files_changed": ["a.c"], "forwarded": "no", "confidence": 0.9,
        "notes": "", "debdiff": str(debdiff), "debdiff_lines": 1})
    result(app, iid, "verify", "ok", {"outcome": "built",
                                      "version": ver + "ubuntu1"})
    app.db.execute("INSERT INTO gate VALUES (?, 'reproduce', 'pending',"
                   " NULL, NULL, ?)", (iid, now()))
    # an agent in flight in the running run
    adir = app.config.work_dir / src / ver / "amd64" / "dev" / "attempt-2"
    adir.mkdir(parents=True)
    (adir / "transcript.jsonl").write_text(
        json.dumps({"type": "system", "subtype": "init", "model": "m",
                    "cwd": str(adir), "tools": ["Read"]}) + "\n")
    app.db.event("unit_start", run_id=run_id, stage="dev", unit=iid)
    app.db.event("agent_call_start", run_id=run_id, stage="dev", unit=iid,
                 model="m", tier="medium", attempt_dir=str(adir))
    return {"app": app, "root": root, "iid": iid, "src": src, "ver": ver,
            "run_id": run_id, "adir": adir}


@pytest.fixture(scope="module")
def client(project):
    return TestClient(create_app(project["root"]))


def test_report_stitches_stage_partials(project):
    app = project["app"]
    text = app.reporter.report(project["src"], project["ver"])
    assert text.startswith(f"# {project['src']} {project['ver']}")
    assert "**Upload the verified fix**" in text
    heads = [ln for ln in text.splitlines() if ln.startswith("## ")]
    assert heads.index("## Failure excerpt") < heads.index("## Triage") \
        < heads.index("## Diagnosis") < heads.index("## Proposed fix") \
        < heads.index("## Verification")
    assert "````\nboom ``` fence\n````" in text  # fence cannot break
    assert "a \\| b" in text  # table cell escaped
    assert "+fixed line" in text  # debdiff embedded
    assert "Review the reproduce gate" in text


def test_plugin_partial_overrides_builtin(project, tmp_path):
    from ftbfs.report import Reporter

    (tmp_path / "templates" / "stages").mkdir(parents=True)
    (tmp_path / "templates" / "stages" / "verify.md.j2").write_text(
        "## My verify\n")
    app = project["app"]
    text = Reporter(app.db, app.pipeline, tmp_path).report(
        project["src"], project["ver"])
    assert "## My verify" in text and "## Verification" not in text


def test_pages_render(client, project):
    src, ver = project["src"], project["ver"]
    for url in ["/", "/runs", f"/runs/{project['run_id']}",
                f"/runs/{project['run_id']}/panel", "/items",
                "/items?stage=verify&status=ok", f"/pkg/{src}/{ver}",
                f"/pkg/{src}/{ver}/investigation.md", "/clusters",
                "/cluster?id=c1", "/signals", "/gates", "/attention", "/costs",
                "/snapshots",
                f"/console?dir={quote(str(project['adir']))}"]:
        r = client.get(url)
        assert r.status_code == 200, url
    page = client.get(f"/pkg/{src}/{ver}").text
    # LLM text is escaped, never injected as markup
    assert "<script>alert" not in page and "&lt;script&gt;" in page
    items = client.get("/items?stage=verify&status=ok&everything=1").text
    assert src in items


def test_signals_visible(client, project):
    src = project["src"]
    page = client.get("/signals?signal=sync-candidate&everything=1").text
    assert "Debian unstable is newer" in page
    assert src in page and "#1234" in page and "99.0-1" in page
    assert "javascript:" not in page  # only http(s) upstream links
    assert src in client.get("/items?signal=debian-patch&everything=1").text
    assert src not in client.get(
        "/items?signal=not-in-debian&everything=1").text
    assert "c1" in client.get("/clusters?signal=sync-candidate").text
    assert "c1" not in client.get("/clusters?signal=not-in-debian").text
    pkg = client.get(f"/pkg/{src}/{project['ver']}").text
    assert "/signals?signal=debian-patch" in pkg


def test_live_run_shows_agent_in_flight(project):
    s = q.run_state(project["app"].db, project["run_id"])
    assert [a["unit"] for a in s["agents"]] == [project["iid"]]
    assert s["stages"]["dev"]["running"] == 1


def test_gate_decision_from_ui(client, project):
    app, iid = project["app"], project["iid"]
    r = client.post("/gates/decide", headers=HX, data={
        "stage": "reproduce", "unit": iid, "decision": "approved"})
    assert r.status_code == 200 and "approved" in r.text
    gate = app.db.one("SELECT * FROM gate WHERE unit_id=? AND"
                      " stage='reproduce'", (iid,))
    assert gate["decision"] == "approved" and gate["by"] == "web"
    assert app.db.one("SELECT 1 FROM event WHERE type='gate_approved'"
                      " AND unit=?", (iid,))
    r = client.post("/retry", headers=HX, data={"stage": "dev",
                                                "unit": iid})
    assert r.status_code == 200
    assert app.db.one("SELECT 1 FROM event WHERE type='retry' AND"
                      " unit=? AND stage='dev'", (iid,))


def test_guards(client, project):
    assert client.post("/retry", data={"stage": "dev", "unit": "x"}
                       ).status_code == 403  # no HX-Request
    assert client.get("/", headers={"host": "evil.example"}
                      ).status_code == 403
    assert client.get("/file?path=/etc/passwd").status_code == 403
    assert client.get("/file?path=work/../config.toml").status_code == 403
    t = quote(str(project["adir"] / "transcript.jsonl"))
    assert client.get(f"/file?path={t}").status_code == 200
    r = client.post("/console/kill", headers=HX,
                    data={"dir": str(project["adir"])})
    assert "not running" in r.text


def test_snapshot_diff_new_gone_regressed(project, tmp_path):
    app = project["app"]
    snap1 = app.db.one("SELECT * FROM snapshot ORDER BY id LIMIT 1")
    data = json.loads(Path(snap1["path"]).read_text())
    pkgs = data["packages"]
    dropped = pkgs.pop(0)

    def write(name, packages):
        path = tmp_path / name
        path.write_text(json.dumps({**data, "packages": packages}))
        return app.db.execute(
            "INSERT INTO snapshot (series, fetched_at, source_url, path)"
            " VALUES ('s', 't', 'u', ?)", (str(path),)).lastrowid

    a = write("a.json", pkgs)
    b = write("b.json", [dropped, *pkgs[1:]])
    d = q.snapshot_diff(app.db, a, b)
    ids = {f"{dropped['source']}/{v['version']}/{x['arch']}"
           for v in dropped["versions"] for x in v["builds"]}
    assert set(d["regressed"]) == ids  # failed in snapshot 1, back in b
    assert d["new"] == []
    assert pkgs[0]["source"] in d["fixed_sources"]


def test_transcript_entries_and_partial_lines(tmp_path):
    path = tmp_path / "t.jsonl"
    lines = [
        {"type": "system", "subtype": "init", "model": "m", "cwd": "/w",
         "tools": ["Read"]},
        {"type": "system", "subtype": "thinking_tokens"},
        {"type": "assistant", "message": {"content": [
            {"type": "tool_use", "id": "t1", "name": "Read",
             "input": {"file_path": "/w/src/a.c"}}]}},
        {"type": "user", "message": {"content": [
            {"type": "tool_result", "tool_use_id": "t1",
             "content": "int x;", "is_error": False}]}},
        {"type": "result", "num_turns": 2, "total_cost_usd": 0.01,
         "is_error": True, "result": "API 429"},
    ]
    body = "".join(json.dumps(x) + "\n" for x in lines)
    path.write_text(body + '{"type": "assist')  # line still being written
    state: dict = {}
    entries, offset = transcript.read(path, 0, state)
    assert [e.kind for e in entries] == ["start", "tool", "result",
                                         "final"]
    assert entries[1].body == "src/a.c"  # relative to the agent's cwd
    assert entries[2].title == "Read result"
    assert entries[3].error and "API 429" in entries[3].body
    assert offset == len(body.encode())
    more, _ = transcript.read(path, offset, state)
    assert more == []


def test_kill_agent_only_kills_its_own_process(project):
    import subprocess

    app, adir = project["app"], project["adir"]
    proc = subprocess.Popen(["sleep", "30"], start_new_session=True)
    (adir / "pid").write_text(str(proc.pid))
    assert app.kill_agent(adir, "test") == proc.pid
    assert proc.wait(timeout=5) == -9
    assert app.db.one("SELECT 1 FROM event WHERE type='agent_killed'")
    # the pid file outlives the process: nothing left to kill
    with pytest.raises(LookupError):
        app.kill_agent(adir, "test")
    with pytest.raises(ValueError):
        app.kill_agent(Path("/tmp"), "test")
