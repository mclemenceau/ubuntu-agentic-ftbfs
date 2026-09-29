"""LXD builder offline: a fake `Lxd` maps each container's filesystem
to a local directory and runs its commands locally, with a fake sbuild
that writes a timestamped .build log and its symlink like sbuild."""

import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ftbfs.builder import local
from ftbfs.builder.lxd import (
    HOME,
    Lxd,
    LxdBuilder,
    LxdError,
    dsc_files,
    image_script,
)

FAKE_SBUILD = """#!/bin/sh
dsc=$(eval echo \\${$#})
name=$(basename "$dsc" .dsc)
[ -f "$dsc" ] && [ -f "${name%_*}_1.0.orig.tar.gz" ] || exit 99
echo "parallel: $DEB_BUILD_OPTIONS verbose: $1"
log="${name}_amd64-2026-01-01T00:00:00Z.build"
echo "E: Build failure" > "$log"
ln -sf "$log" "${name}_amd64.build"
[ -n "$HANG" ] && sleep 60
exit ${FAKE_SBUILD_EXIT:-2}
"""

DSC = """\
-----BEGIN PGP SIGNED MESSAGE-----

Source: p
Version: 1.0-1
Checksums-Sha256:
 abc 10 p_1.0.orig.tar.gz
Files:
 d41d8cd98f00b204e9800998ecf8427e 10 p_1.0.orig.tar.gz
 d41d8cd98f00b204e9800998ecf8427e 20 p_1.0-1.debian.tar.xz

-----BEGIN PGP SIGNATURE-----
"""


class FakeLxd(Lxd):
    def __init__(self, remote, root: Path, image: str | None = "fp1"):
        super().__init__(remote)
        self.root = root
        self.images = {"ftbfs-builder": image} if image else {}
        self.instances: dict[str, dict] = {}
        self.calls: list[tuple] = []

    def path(self, name, p: str) -> Path:
        return self.root / name / p.lstrip("/")

    def instance(self, name):
        return self.instances.get(name)

    def image(self, alias):
        return self.images.get(alias)

    def launch(self, image, name, config):
        self.calls.append(("launch", name, image))
        self.path(name, HOME).mkdir(parents=True)
        self.instances[name] = {"status": "Running", "config": {
            "volatile.base_image": image, **config}}

    def delete(self, name):
        self.calls.append(("delete", name))
        shutil.rmtree(self.root / name)
        del self.instances[name]

    def restart(self, name):
        self.calls.append(("restart", name))

    def start(self, name):
        self.calls.append(("start", name))
        self.instances[name]["status"] = "Running"

    def exec_argv(self, name, argv, *, user, env=None, cwd=None):
        home = str(self.path(name, HOME))
        argv = [a.replace(HOME, home) for a in argv]
        cmd = ["env", *(f"{k}={v}" for k, v in (env or {}).items()),
               *argv]
        if cwd:
            cmd = ["sh", "-c", 'cd "$0" && exec "$@"',
                   cwd.replace(HOME, home), *cmd]
        return cmd

    def sh(self, name, script, *, user=False, timeout=600):
        script = script.replace(HOME, str(self.path(name, HOME)))
        proc = subprocess.run(["sh", "-ec", script], capture_output=True,
                              text=True)
        if proc.returncode:
            raise LxdError(proc.stderr)
        return proc.stdout

    def push(self, name, files, dest_dir):
        for f in files:
            shutil.copy(f, self.path(name, dest_dir))

    def pull(self, name, src, dest):
        shutil.copy(self.path(name, src.replace(
            str(self.path(name, HOME)), HOME)), dest)


@pytest.fixture
def env(tmp_path, monkeypatch):
    bindir = tmp_path / "bin"
    bindir.mkdir()
    exe = bindir / "sbuild"
    exe.write_text(FAKE_SBUILD)
    exe.chmod(exe.stat().st_mode | stat.S_IEXEC)
    monkeypatch.setenv("PATH", f"{bindir}:{shutil.os.environ['PATH']}")
    src = tmp_path / "src"
    src.mkdir()
    (src / "p_1.0-1.dsc").write_text(DSC)
    (src / "p_1.0.orig.tar.gz").write_text("orig")
    (src / "p_1.0-1.debian.tar.xz").write_text("debian")
    return tmp_path, src / "p_1.0-1.dsc"


def builder(tmp_path, fake=None, slots=1) -> LxdBuilder:
    b = LxdBuilder("t", slots=slots, remote="r")
    b.lxd = fake or FakeLxd("r", tmp_path / "hosts")
    b._wait_ready = lambda w: None
    return b


def go(b, dsc, build_dir, **kw):
    opts = {"timeout": 60, "idle_timeout": 60, **kw}
    return b.build(dsc, "amd64", "d-proposed", build_dir, [], 6, **opts)


def test_first_build_launches_worker_and_pulls_the_log(env):
    tmp, dsc = env
    b = builder(tmp)
    res = go(b, dsc, tmp / "out")
    assert not res.ok and res.exit_code == 2 and not res.timed_out
    assert res.log == tmp / "out" / "p_1.0-1_amd64-2026-01-01T00:00:00Z.build"
    assert res.log.read_text() == "E: Build failure\n"
    assert b.lxd.calls == [("launch", "ftbfs-t-1", "fp1")]
    out = (tmp / "out" / "sbuild.out").read_text()
    assert "parallel: parallel=6 verbose: --verbose" in out
    assert (tmp / "out" / "worker.txt").read_text() == "r:ftbfs-t-1\n"


def test_worker_reuse_restart_and_image_change(env):
    tmp, dsc = env
    fake = FakeLxd("r", tmp / "hosts")
    b = builder(tmp, fake)
    go(b, dsc, tmp / "a")
    go(b, dsc, tmp / "b")  # same process: no restart
    assert fake.calls == [("launch", "ftbfs-t-1", "fp1")]
    # A new process restarts a worker it finds running: a crashed run
    # may have left a build in it.
    go(builder(tmp, fake), dsc, tmp / "c")
    assert fake.calls[-1] == ("restart", "ftbfs-t-1")
    fake.images["ftbfs-builder"] = "fp2"
    go(b, dsc, tmp / "d")
    assert fake.calls[-2:] == [("delete", "ftbfs-t-1"),
                               ("launch", "ftbfs-t-1", "fp2")]


def test_stopped_worker_is_started(env):
    tmp, dsc = env
    fake = FakeLxd("r", tmp / "hosts")
    go(builder(tmp, fake), dsc, tmp / "a")
    fake.instances["ftbfs-t-1"]["status"] = "Stopped"
    go(builder(tmp, fake), dsc, tmp / "b")
    assert fake.calls[-1] == ("start", "ftbfs-t-1")


def test_no_image_is_an_error(env):
    tmp, dsc = env
    b = builder(tmp, FakeLxd("r", tmp / "hosts", image=None))
    with pytest.raises(LxdError, match="ftbfs builders image"):
        go(b, dsc, tmp / "out")


def test_stalled_build_is_killed_and_the_worker_restarted(env,
                                                          monkeypatch):
    tmp, dsc = env
    monkeypatch.setenv("HANG", "1")
    monkeypatch.setattr(local, "POLL_S", 0.1)
    fake = FakeLxd("r", tmp / "hosts")
    b = builder(tmp, fake)
    res = go(b, dsc, tmp / "out", idle_timeout=1)
    assert res.timed_out and res.stalled and not res.ok
    assert fake.calls[-1] == ("restart", "ftbfs-t-1")
    assert res.log is not None  # the partial log is still pulled


def test_each_slot_has_its_own_worker(env):
    tmp, _ = env
    b = builder(tmp, slots=2)
    with b._worker() as w1, b._worker() as w2:
        assert {w1, w2} == {"ftbfs-t-1", "ftbfs-t-2"}


def test_another_process_waits_for_the_worker(env):
    """Two processes (two runs) never build in one worker at once."""
    tmp, _ = env
    locks = tmp / "locks"
    code = (
        "import sys, time\n"
        "from ftbfs.builder.lxd import LxdBuilder\n"
        "b = LxdBuilder('t', slots=1, remote='r',"
        f" lock_dir=__import__('pathlib').Path({str(locks)!r}))\n"
        "with b._worker():\n"
        "    print('locked', flush=True)\n"
        "    time.sleep(1)\n")
    other = subprocess.Popen([sys.executable, "-c", code],
                             stdout=subprocess.PIPE, text=True)
    assert other.stdout.readline() == "locked\n"
    b = builder(tmp)
    b.lock_dir = locks
    start = time.monotonic()
    with b._worker() as w:
        waited = time.monotonic() - start
    other.wait(5)
    assert w == "ftbfs-t-1" and waited > 0.5


def test_builder_name_must_be_a_valid_container_name():
    with pytest.raises(ValueError, match="lowercase"):
        LxdBuilder("My_Host", slots=1, remote="r")


def test_dsc_files(env):
    _, dsc = env
    assert [p.name for p in dsc_files(dsc)] == [
        "p_1.0-1.dsc", "p_1.0.orig.tar.gz", "p_1.0-1.debian.tar.xz"]


def test_image_script():
    script = image_script("stonking", ["amd64", "i386"])
    assert "sed -i 's/flags=(/&complain /' /etc/apparmor.d/sbuild" \
        in script
    assert script.count("mmdebstrap --mode=unshare") == 2
    assert f"{HOME}/.cache/sbuild/stonking-proposed-i386.tar" in script
    assert "stonking-proposed main universe" in script
