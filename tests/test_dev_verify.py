# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""dev + verify loop offline: a real tiny 3.0 (quilt) source package, a
fake agent editing the tree, a fake sbuild failing once then passing."""

import json
import os
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
    db.execute("INSERT INTO gate (unit_id, stage, decision, by, note, ts)"
               " VALUES ('tinypkg/1.0-1/amd64','dev',"
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
    """Edit the tree like a real agent would; on retry, add a second fix
    on top of the first one, which must still be in the tree."""
    main = req.cwd / "src" / "main.c"
    if '"retry"' in req.prompt:
        assert main.read_text().startswith("const char")
        (req.cwd / "src" / "extra.h").write_text("int extra(void);\n")
    else:
        main.write_text("const char *name = \"x\";\n")
    return {"summary": "Fix FTBFS with GCC 15 (const string).",
            "patch_name": "gcc 15 const name",
            "patch_description": "Make name const. Needed with GCC 15.",
            "forwarded": "no", "bug_debian": None,
            "files_changed": ["src/main.c"], "confidence": 0.8,
            "notes": ""}


IDENTITY = {"name": "Test Dev", "email": "dev@example.org"}


def scheduler(db, item, tmp, identity=IDENTITY, responder=agent,
              reproduced="reproduced"):
    rep = type("Rep", (Seed,), {"name": "reproduce",
                                "payload": {"outcome": reproduced}})
    exc = type("Exc", (Seed,), {"name": "excerpt", "payload": {
        "signature": "orig", "key_lines": ["e"], "text": "t"}})
    registry = {**discover(), "reproduce": rep, "excerpt": exc}
    conf = {"excerpt": {}, "reproduce": {"after": ["excerpt"]},
            "dev": {"after": ["reproduce"], "gate": "manual",
                    "agent": {"tier": "medium", "escalate_after_loops": 1}},
            "verify": {"after": ["dev"],
                       "on_fail": {"goto": "dev", "max_loops": 2}}}
    backend = FakeBackend(responder=responder)
    pipeline = build({"stage": conf}, registry, "fake")
    paths = Paths(tmp, tmp / "work", tmp / "cache")
    return Scheduler(db, pipeline, Units([item], db), {"fake": backend},
                     paths, identity=identity), backend


def run(db, item, tmp, identity=IDENTITY, responder=agent, **kw):
    sched, backend = scheduler(db, item, tmp, identity, responder, **kw)
    return sched.run(), backend


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
    assert patch.startswith("Description: Fix FTBFS with GCC 15 (const"
                            " string)\n Make name const. Needed with"
                            " GCC 15.\n")
    assert "Author: Test Dev <dev@example.org>" in patch
    # the retry kept the first fix and added the second: one patch
    assert "+const char *name" in patch
    assert "+int extra(void);" in patch
    assert dev["files_changed"] == ["src/extra.h", "src/main.c"]
    debdiff = Path(dev["debdiff"]).read_text()
    assert "tinypkg (1.0-1ubuntu1) stonking" in debdiff
    assert "XSBC-Original-Maintainer: Someone" in debdiff  # first delta
    assert latest(db, "verify")[1]["outcome"] == "built"


def test_verified_fix_outdated_by_a_new_reproduction(env, monkeypatch):
    """A verified fix is current while dev and verify would keep their
    results. A new reproduction (here: Launchpad's build now fails
    otherwise) reopens dev's gate, bound to the old inputs: the fix is
    for a failure that is gone."""
    db, item, tmp = env
    monkeypatch.setenv("FAIL_TIMES", "0")
    run(db, item, tmp)
    assert latest(db, "verify")[0] == "ok"
    uid_for = {"item": item["id"], "package": item["source"]}
    sched, _ = scheduler(db, item, tmp)
    assert sched.outdated(uid_for, "verify") == []

    db.event("retry", stage="reproduce", unit=item["id"])
    totals, _ = run(db, item, tmp, reproduced="different-failure")
    assert totals["reproduce"] == {"ok": 1} and "dev" not in totals
    assert latest(db, "verify")[0] == "ok"  # still the old verify
    sched, _ = scheduler(db, item, tmp)
    assert sched.outdated(uid_for, "verify") == [
        ("dev", "waiting for manual approval")]


def test_loop_gives_up_after_max_loops(env, monkeypatch):
    db, item, tmp = env
    monkeypatch.setenv("FAIL_TIMES", "99")
    totals, _ = run(db, item, tmp)
    assert totals["dev"] == {"ok": 3} and totals["verify"] == {"fail": 3}
    assert db.one("SELECT 1 FROM event WHERE type='loop_exhausted'")


def test_dev_without_identity_fails_before_the_agent(env, monkeypatch):
    db, item, tmp = env
    monkeypatch.setenv("GIT_CONFIG_GLOBAL", "/dev/null")
    monkeypatch.setenv("GIT_CONFIG_SYSTEM", "/dev/null")
    monkeypatch.setenv("GIT_CEILING_DIRECTORIES", str(tmp.parent))
    totals, backend = run(db, item, tmp, identity={})
    assert list(totals["dev"]) == ["error"]
    assert backend.calls == []
    assert "[identity]" in latest(db, "dev")[1]["error"]


def hostile_git_config(marker: Path) -> str:
    """A git config that runs a command on the next `git add`."""
    hook = marker.parent / "hook.sh"
    hook.write_text(f"#!/bin/sh\ntouch {marker}\n")
    hook.chmod(0o755)
    return f"[core]\n\tfsmonitor = {hook}\n\thooksPath = {hook.parent}\n"


def test_git_config_written_in_the_tree_runs_nothing(env, monkeypatch):
    """The tree is the agent's to edit (and comes from an untrusted
    package): a .git/config in it is just a file."""
    db, item, tmp = env
    monkeypatch.setenv("FAIL_TIMES", "0")
    marker = tmp / "pwned"

    def plant(req):
        (req.cwd / ".git").mkdir(exist_ok=True)
        (req.cwd / ".git" / "config").write_text(hostile_git_config(marker))
        return agent(req)

    totals, _ = run(db, item, tmp, responder=plant)
    assert totals["dev"] == {"ok": 1}
    assert not marker.exists()
    assert ".git/config" not in latest(db, "dev")[1]["files_changed"]


def test_dev_refuses_a_repository_changed_by_the_agent(env, monkeypatch):
    """An agent that reaches the repository next to the tree (a backend
    without path confinement) gets the unit failed, and git never runs
    its config."""
    db, item, tmp = env
    marker = tmp / "pwned"

    def escape(req):
        gd = srcpkg.git_dir(req.cwd)
        with (gd / "config").open("a") as f:
            f.write(hostile_git_config(marker))
        return agent(req)

    totals, _ = run(db, item, tmp, responder=escape)
    assert list(totals["dev"]) == ["error"]
    assert "changed during the agent run" in latest(db, "dev")[1]["error"]
    assert not marker.exists()


def test_next_ubuntu_version():
    assert srcpkg.next_ubuntu_version("1.0-2") == "1.0-2ubuntu1"
    assert srcpkg.next_ubuntu_version("1.0-2build3") == "1.0-2ubuntu1"
    assert srcpkg.next_ubuntu_version("1.0-2ubuntu4") == "1.0-2ubuntu5"
    assert srcpkg.next_ubuntu_version("20.2.1-0ubuntu3") == \
        "20.2.1-0ubuntu4"


def test_links_out_of_the_tree_are_hidden_from_the_agent(tmp_path):
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "secret").write_text("key\n")
    tree = tmp_path / "tree"
    (tree / "src").mkdir(parents=True)
    (tree / "src" / "real.c").write_text("int x;\n")
    (tree / "src" / "inside.c").symlink_to("real.c")
    (tree / "config.guess").symlink_to("/usr/share/misc/config.guess")
    (tree / "src" / "up").symlink_to("../../outside")
    (tree / "loop").symlink_to("loop")
    links = srcpkg.hide_links(tree)
    assert links == {"config.guess": "/usr/share/misc/config.guess",
                     "src/up": "../../outside", "loop": "loop"}
    assert (tree / "src" / "inside.c").is_symlink()  # stays
    assert not (tree / "src" / "up").exists()
    srcpkg.restore_links(tree, links)
    assert os.readlink(tree / "src" / "up") == "../../outside"
    assert os.readlink(tree / "config.guess") == \
        "/usr/share/misc/config.guess"

    # the agent writes where a link was: refused, nothing written outside
    links = srcpkg.hide_links(tree)
    (tree / "src" / "up").mkdir()
    (tree / "src" / "up" / "secret").write_text("overwritten\n")
    with pytest.raises(srcpkg.SourceError, match="src/up"):
        srcpkg.restore_links(tree, links)
    assert (outside / "secret").read_text() == "key\n"


@pytest.mark.parametrize("path", ["debian/changelog", "debian/patches",
                                  ".pc"])
def test_packaging_links_out_of_the_tree_are_refused(tmp_path, path):
    """The steps after the agent (dch, quilt, the patch file) write
    there: through such a link they would write outside the tree."""
    tree = tmp_path / "tree"
    (tree / "debian").mkdir(parents=True)
    (tree / path).symlink_to(tmp_path)
    with pytest.raises(srcpkg.SourceError, match="outside the source"):
        srcpkg.hide_links(tree)


def changelog_entry(debdiff: str) -> list[str]:
    """The new changelog entry's lines, from the debdiff."""
    out = []
    for line in debdiff.split("+++ ", 2)[1].splitlines()[2:]:
        if not line.startswith("+"):
            break
        out.append(line[1:])
    return out


KAPTAN = {
    "summary": "Fix FTBFS with Python 3.15 by replacing removed"
               " SourceFileLoader.load_module().",
    "patch_name": "python315-remove-load-module",
    "patch_description": (
        "Python 3.15 removed the long-deprecated"
        " SourceFileLoader.load_module() method, causing kaptan's Python"
        " config file handler to fail with AttributeError. Replace it"
        " with the modern importlib recipe (spec_from_loader,"
        " module_from_spec, exec_module) so config files can still be"
        " loaded dynamically."),
    "forwarded": "no", "bug_debian": "https://bugs.debian.org/1149335",
    "files_changed": ["src/main.c"], "confidence": 0.9, "notes": ""}


def test_one_changelog_item_naming_the_patch_and_debian_bug(env,
                                                           monkeypatch):
    """python-kaptan: the entry had the summary and the description's
    first sentence as two items saying the same thing, and no Debian
    bug. One item: the patch, the summary, the bug as plain text."""
    db, item, tmp = env
    monkeypatch.setenv("FAIL_TIMES", "0")

    def kaptan(req):
        agent(req)
        return KAPTAN

    run(db, item, tmp, responder=kaptan)
    dev = latest(db, "dev")[1]
    entry = changelog_entry(Path(dev["debdiff"]).read_text())
    assert entry[2:6] == [
        "  * d/p/python315-remove-load-module.patch: Fix FTBFS with Python"
        " 3.15 by",
        "    replacing removed SourceFileLoader.load_module() (Debian"
        " #1149335).",
        "",
        " -- Test Dev <dev@example.org>  " + entry[5].split(">  ")[1],
    ]
    assert "Closes" not in "\n".join(entry)


def test_dep3_description_is_a_synopsis_and_a_wrapped_body(env,
                                                           monkeypatch):
    """python-kaptan: Description was one 307-column line. The summary
    is the synopsis; the description follows on continuation lines
    within 80 columns."""
    db, item, tmp = env
    monkeypatch.setenv("FAIL_TIMES", "0")

    def kaptan(req):
        agent(req)
        return {**KAPTAN, "summary": "Stop using load_module(), removed"
                                     " in Python 3.15."}

    run(db, item, tmp, responder=kaptan)
    patch = Path(latest(db, "dev")[1]["patch_file"]).read_text()
    header = patch.split("\n---\n")[0].splitlines()
    assert header[0] == ("Description: Stop using load_module(), removed"
                         " in Python 3.15")
    body = header[1:header.index("Author: Test Dev <dev@example.org>")]
    assert body[0] == " Python 3.15 removed the long-deprecated" \
                      " SourceFileLoader.load_module() method,"
    assert all(ln.startswith(" ") and len(ln) <= 80 for ln in body)
    assert " ".join(ln.strip() for ln in body) == \
        KAPTAN["patch_description"]


def test_packaging_only_fix_names_the_files_it_changes(env, monkeypatch):
    """osmnx: a debian/rules-only fix got "debian/rules: see patch
    description." when there is no patch."""
    db, item, tmp = env
    monkeypatch.setenv("FAIL_TIMES", "0")

    def rules(req):
        with (req.cwd / "debian" / "rules").open("a") as f:
            f.write("\noverride_dh_python3:\n\tdh_python3\n")
        return {**KAPTAN, "summary": "Do not ship upstream tests/ as a"
                                     " top-level module",
                "bug_debian": "1151535",
                "files_changed": ["debian/rules"]}

    run(db, item, tmp, responder=rules)
    dev = latest(db, "dev")[1]
    assert dev["patch_file"] is None
    entry = changelog_entry(Path(dev["debdiff"]).read_text())
    assert entry[2:4] == [
        "  * d/rules: Do not ship upstream tests/ as a top-level module"
        " (Debian",
        "    #1151535).",
    ]


def test_changelog_wraps_at_80_columns_without_breaking_options(
        env, monkeypatch):
    """tiledarray: dch wrapped "-lze_loader" as "-" and "lze_loader"."""
    db, item, tmp = env
    monkeypatch.setenv("FAIL_TIMES", "0")

    def flags(req):
        agent(req)
        return {**KAPTAN, "bug_debian": None,
                "patch_name": "fix-ftbfs-mpi-libs-cstdint-tests",
                "summary": "Remove -lucc, -lze_loader, -lucp and -lucs"
                           " from the pkg-config MPI link flags.",
                "patch_description": "Remove -lucc, -lze_loader, -lucp"
                " and -lucs from the pkg-config MPI link flags. Those"
                " libraries are not in the archive."}

    run(db, item, tmp, responder=flags)
    dev = latest(db, "dev")[1]
    entry = changelog_entry(Path(dev["debdiff"]).read_text())
    items = entry[2:entry.index("", 2)]
    assert all(len(ln) <= 80 for ln in items)
    assert not any(ln.endswith("-") for ln in items)
    log = subprocess.run(
        ["dpkg-parsechangelog", "-l", str(Path(dev["new_dsc"]).parent /
                                          "tree" / "debian" / "changelog"),
         "-S", "Changes"], capture_output=True, text=True, check=True)
    assert "tinypkg (1.0-1ubuntu1) stonking; urgency=medium" in log.stdout
