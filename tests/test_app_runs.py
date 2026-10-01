# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

import os
import socket
import subprocess
import time

import pytest

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


def test_start_run_spawns_a_detached_run(tmp_path):
    (tmp_path / "pipeline.toml").write_text("[stage.excerpt]\n")
    app = App(tmp_path)
    run_id = app.start_run(until="excerpt", by="web")
    run = app.db.one("SELECT * FROM run WHERE id=?", (run_id,))
    assert run["trigger"] == "web"
    for _ in range(100):  # nothing is selected, so it ends quickly
        if status(app, run_id) != "running":
            break
        time.sleep(0.1)
    assert status(app, run_id) == "done"
    assert app.db.one("SELECT 1 FROM event WHERE type='run_requested'")
    insert_run(app, os.getpid(), socket.gethostname())
    with pytest.raises(ValueError, match="already in progress"):
        app.start_run()


def test_decided_versions_are_left_out_of_runs(tmp_path, snapshot):
    from ftbfs.inventory import store

    (tmp_path / "pipeline.toml").write_text("[stage.excerpt]\n")
    app = App(tmp_path)
    store(app.db, snapshot, tmp_path / "s.json")
    flt = app.config.make_filter(sources=["freehsm-c"])
    item = app.select(flt)[0]
    app.dispose(item["source"], item["version"], "accepted", "test")
    left = app.workable(flt)
    assert len(left) < len(app.select(flt))
    assert item["version"] not in {r["version"] for r in left}
    why = app.explain([item["id"]], flt)[item["id"]]
    assert any("you decided: accepted" in r for r in why["filtered_out"])
    with pytest.raises(ValueError):
        app.dispose(item["source"], item["version"], "bogus", "test")
    app.dispose(item["source"], item["version"], None, "test")
    assert len(app.workable(flt)) == len(app.select(flt))


def test_tail_shows_the_last_matching_events(tmp_path, capsys):
    from ftbfs.cli import main

    app = App(tmp_path)
    for i in range(5):
        app.db.event("unit_end", unit="a/1/amd64", stage="verify", n=i)
    for _ in range(50):
        app.db.event("unit_end", unit="b/1/amd64", stage="excerpt")
    main(["--root", str(tmp_path), "tail", "-n", "2",
          "--unit", "a/1/amd64", "--stage", "verify"])
    lines = capsys.readouterr().out.splitlines()
    assert len(lines) == 2
    assert "n=3" in lines[0] and "n=4" in lines[1]


def test_cli_output_piped_into_head(tmp_path):
    import sys

    app = App(tmp_path)
    for i in range(5000):
        app.db.event("unit_end", unit=f"p/{i}/amd64", stage="excerpt")
    proc = subprocess.Popen(
        [sys.executable, "-m", "ftbfs", "--root", str(tmp_path), "tail",
         "-n", "5000"],
        stdout=subprocess.PIPE, stderr=subprocess.PIPE)
    proc.stdout.readline()
    proc.stdout.close()  # what `| head -1` does
    err = proc.stderr.read().decode()
    proc.wait(timeout=30)
    assert "Traceback" not in err, err
