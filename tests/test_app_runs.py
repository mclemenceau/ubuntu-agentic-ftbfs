import os
import socket
import subprocess

from ftbfs.app import App


def insert_run(app, pid, host, ts="2026-09-27T00:00:00+00:00"):
    run_id = app.db.execute(
        "INSERT INTO run (started, status, filter, pipeline_hash, trigger,"
        " pid, host) VALUES (?, 'running', '{}', 'h', 'test', ?, ?)",
        (ts, pid, host),
    ).lastrowid
    app.db.event("run_start", run_id=run_id)
    return run_id


def status(app, run_id):
    return app.db.one("SELECT status FROM run WHERE id=?",
                      (run_id,))["status"]


def test_reap_stale_runs(tmp_path):
    app = App(tmp_path)
    host = socket.gethostname()
    proc = subprocess.Popen(["true"])
    proc.wait()  # a pid that is now dead
    dead = insert_run(app, proc.pid, host)
    alive = insert_run(app, os.getpid(), host)
    elsewhere = insert_run(app, proc.pid, "other-host")
    legacy_fresh = insert_run(app, None, None)  # just had an event

    assert app.reap_stale_runs() == [dead]
    assert status(app, dead) == "interrupted"
    assert status(app, alive) == "running"
    assert status(app, elsewhere) == "running"  # can't check other hosts
    assert status(app, legacy_fresh) == "running"
    assert app.db.one("SELECT 1 FROM event WHERE type='run_interrupted'"
                      " AND run_id=?", (dead,))


def test_legacy_run_reaped_when_silent(tmp_path):
    app = App(tmp_path)
    run_id = app.db.execute(
        "INSERT INTO run (started, status, filter, pipeline_hash, trigger)"
        " VALUES ('2026-01-01T00:00:00+00:00', 'running', '{}', 'h', 't')"
    ).lastrowid
    app.db.execute(
        "INSERT INTO event (run_id, ts, type, payload)"
        " VALUES (?, '2026-01-01T00:00:00+00:00', 'run_start', '{}')",
        (run_id,),
    )
    assert app.reap_stale_runs() == [run_id]


def test_approve_expands_source_to_waiting_items(tmp_path, snapshot):
    from ftbfs.cli import main
    from ftbfs.db import now
    from ftbfs.inventory import store

    (tmp_path / "pipeline.toml").write_text(
        '[stage.excerpt]\n[stage.reproduce]\nafter = ["excerpt"]\n'
        'gate = "manual"\n')
    app = App(tmp_path)
    store(app.db, snapshot, tmp_path / "s.json")
    ids = [r["id"] for r in app.db.query(
        "SELECT id FROM item WHERE source='freehsm-c' ORDER BY id")]
    assert len(ids) > 1
    app.db.execute("INSERT INTO gate VALUES (?, 'reproduce', 'pending',"
                   " NULL, NULL, ?)", (ids[0], now()))
    main(["--root", str(tmp_path), "approve", "reproduce", "freehsm-c"])
    approved = [r["unit_id"] for r in app.db.query(
        "SELECT unit_id FROM gate WHERE decision='approved'")]
    assert approved == [ids[0]]
