"""dev + verify loop offline: a real tiny 3.0 (quilt) source package, a
fake agent editing the tree, a fake sbuild failing once then passing."""

import json
import shutil
import stat
import subprocess
import tarfile
from pathlib import Path

import pytest

from ftbfs.agents.fake import FakeBackend
from ftbfs.builder import srcpkg
from ftbfs.core.context import Paths, Units
from ftbfs.core.pipeline import build
from ftbfs.core.scheduler import Scheduler
from ftbfs.core.stage import (
    Kind,
    Stage,
    StageResult,
    Status,
    UnitType,
    discover,
)
from ftbfs.db import now

ROOT = Path(__file__).parent.parent
LOG = Path(__file__).parent / "fixtures" / "logs" / "32891842.txt.gz"

pytestmark = pytest.mark.skipif(not shutil.which("dpkg-source"),
                                reason="needs dpkg-dev")

FAKE_SBUILD = """#!/bin/sh
dsc=$(eval echo \\${$#})
name=$(basename "$dsc" .dsc)
n=$(cat "$COUNTER" 2>/dev/null || echo 0); n=$((n+1)); echo $n > "$COUNTER"
if [ "$n" -le "$FAIL_TIMES" ]; then
  cp "$FAKE_SBUILD_LOG" "${name}_amd64.build"; exit 2
fi
echo "Status: successful" > "${name}_amd64.build"; exit 0
"""


def make_source(base: Path) -> Path:
    """tinypkg 1.0-1, 3.0 (quilt), one existing patch."""
    up = base / "tinypkg-1.0"
    (up / "src").mkdir(parents=True)
    (up / "src" / "main.c").write_text("char *name = \"x\";\n")
    with tarfile.open(base / "tinypkg_1.0.orig.tar.gz", "w:gz") as t:
        t.add(up, arcname="tinypkg-1.0")
    deb = up / "debian"
    (deb / "source").mkdir(parents=True)
    (deb / "patches").mkdir()
    (deb / "source" / "format").write_text("3.0 (quilt)\n")
    (deb / "control").write_text(
        "Source: tinypkg\nMaintainer: Someone <s@example.org>\n"
        "Build-Depends: debhelper-compat (= 13)\n\n"
        "Package: tinypkg\nArchitecture: any\nDescription: t\n t\n")
    (deb / "changelog").write_text(
        "tinypkg (1.0-1) unstable; urgency=medium\n\n  * Initial.\n\n"
        " -- Someone <s@example.org>  Mon, 01 Jan 2024 00:00:00 +0000\n")
    (deb / "rules").write_text("#!/usr/bin/make -f\n%:\n\tdh $@\n")
    (deb / "patches" / "series").write_text("")
    subprocess.run(["dpkg-source", "-b", "tinypkg-1.0"], cwd=base,
                   check=True, capture_output=True)
    return base / "tinypkg_1.0-1.dsc"


class Seed(Stage):
    kind, unit = Kind.DETERMINISTIC, UnitType.ITEM
    payload: dict = {}

    def run(self, ctx, ids):
        return [StageResult(i, Status.OK, self.payload) for i in ids]


@pytest.fixture
def env(db, tmp_path, monkeypatch):
    sources = tmp_path / "cache" / "sources" / "tinypkg" / "1.0-1"
    sources.mkdir(parents=True)
    make_source(sources)
    db.execute("INSERT INTO snapshot VALUES (1,'stonking','t','u','p')")
    db.execute("INSERT INTO package VALUES ('tinypkg','universe','[]',"
               "'[]','[]',NULL,NULL,1)")
    db.execute(
        "INSERT INTO item (id, source, version, arch, pocket, state,"
        " build_id, build_url, lifecycle, first_seen, last_seen)"
        " VALUES ('tinypkg/1.0-1/amd64','tinypkg','1.0-1','amd64',"
        "'proposed','FAILEDTOBUILD',1,'u','active',1,1)")
    db.execute("INSERT INTO gate VALUES ('tinypkg/1.0-1/amd64','dev',"
               "'approved','t',NULL,?)", (now(),))
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "sbuild"
    exe.write_text(FAKE_SBUILD)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{shutil.os.environ['PATH']}")
    plain = tmp_path / "fail.build"
    plain.write_text(subprocess.run(["zcat", str(LOG)], capture_output=True,
                                    text=True).stdout)
    monkeypatch.setenv("FAKE_SBUILD_LOG", str(plain))
    monkeypatch.setenv("COUNTER", str(tmp_path / "counter"))
    (tmp_path / "prompts").mkdir()
    shutil.copy(ROOT / "prompts" / "dev.md", tmp_path / "prompts")
    item = dict(db.one("SELECT i.*, p.component FROM item i JOIN package p"
                       " ON p.source=i.source"))
    return db, item, tmp_path


def agent(req):
    """Edit the tree like a real agent would; improve on retry."""
    main = req.cwd / "src" / "main.c"
    retry = '"retry"' in req.prompt
    main.write_text("const char *name = \"x\";\n" +
                    ("/* retried */\n" if retry else ""))
    return {"summary": "Fix FTBFS with GCC 15 (const string).",
            "patch_name": "gcc 15 const name",
            "patch_description": "Make name const. Needed with GCC 15.",
            "forwarded": "no", "bug_debian": None,
            "files_changed": ["src/main.c"], "confidence": 0.8,
            "notes": ""}


def run(db, item, tmp):
    rep = type("Rep", (Seed,), {"name": "reproduce",
                                "payload": {"outcome": "reproduced"}})
    exc = type("Exc", (Seed,), {"name": "excerpt", "payload": {
        "signature": "orig", "key_lines": ["e"], "text": "t"}})
    registry = {**discover(), "reproduce": rep, "excerpt": exc}
    conf = {"excerpt": {}, "reproduce": {"after": ["excerpt"]},
            "dev": {"after": ["reproduce"], "gate": "manual",
                    "name": "Test Dev", "email": "dev@example.org",
                    "agent": {"tier": "medium", "escalate_after_loops": 1}},
            "verify": {"after": ["dev"],
                       "on_fail": {"goto": "dev", "max_loops": 2}}}
    backend = FakeBackend(responder=agent)
    pipeline = build({"stage": conf}, registry, "fake")
    paths = Paths(tmp, tmp / "work", tmp / "cache")
    totals = Scheduler(db, pipeline, Units([item], db), {"fake": backend},
                       paths).run()
    return totals, backend


def latest(db, stage):
    r = db.one("SELECT status, data FROM stage_result WHERE stage=?"
               " ORDER BY id DESC", (stage,))
    return r["status"], json.loads(r["data"])


def test_dev_verify_loop_until_it_builds(env, monkeypatch):
    db, item, tmp = env
    monkeypatch.setenv("FAIL_TIMES", "1")
    totals, backend = run(db, item, tmp)
    assert totals["dev"] == {"ok": 2}
    assert totals["verify"] == {"fail": 1, "ok": 1}
    # the retry prompt carried the verify failure; tier escalated
    assert '"retry"' in backend.calls[1].prompt
    assert backend.calls[0].tier == "medium"
    assert backend.calls[1].tier == "large"

    status, dev = latest(db, "dev")
    assert dev["version"] == "1.0-1ubuntu1"
    patch = Path(dev["patch_file"]).read_text()
    assert patch.startswith("Description: Make name const.")
    assert "Author: Test Dev <dev@example.org>" in patch
    assert "+const char *name" in patch
    debdiff = Path(dev["debdiff"]).read_text()
    assert "tinypkg (1.0-1ubuntu1) stonking" in debdiff
    assert "XSBC-Original-Maintainer: Someone" in debdiff  # first delta
    assert latest(db, "verify")[1]["outcome"] == "built"


def test_loop_gives_up_after_max_loops(env, monkeypatch):
    db, item, tmp = env
    monkeypatch.setenv("FAIL_TIMES", "99")
    totals, _ = run(db, item, tmp)
    assert totals["dev"] == {"ok": 3} and totals["verify"] == {"fail": 3}
    assert db.one("SELECT 1 FROM event WHERE type='loop_exhausted'")


def test_next_ubuntu_version():
    assert srcpkg.next_ubuntu_version("1.0-2") == "1.0-2ubuntu1"
    assert srcpkg.next_ubuntu_version("1.0-2build3") == "1.0-2ubuntu1"
    assert srcpkg.next_ubuntu_version("1.0-2ubuntu4") == "1.0-2ubuntu5"
    assert srcpkg.next_ubuntu_version("20.2.1-0ubuntu3") == \
        "20.2.1-0ubuntu4"
