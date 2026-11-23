# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""LXD builder offline: a fake `Lxd` maps each container's filesystem
to a local directory and runs its commands locally, with a fake sbuild
that writes a timestamped .build log and its symlink like sbuild."""

import json
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from ftbfs.builder import local
from ftbfs.builder.base import BuilderUnavailable
from ftbfs.builder.lxd import (
    HOME,
    Lxd,
    LxdBuilder,
    LxdError,
    build_image,
    copy_image,
    dsc_files,
    image_script,
    series_warning,
)

FAKE_SBUILD = """#!/bin/sh
dsc=$(eval echo \\${$#})
name=$(basename "$dsc" .dsc)
[ -f "$dsc" ] && [ -f "${name%_*}_1.0.orig.tar.gz" ] || exit 99
echo "parallel: $DEB_BUILD_OPTIONS verbose: $1"
if [ -n "$MISSING_CHROOT" ]; then
    echo "E: Chroot for distribution d-proposed, architecture amd64 not found"
    exit 1
fi
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

    def image_properties(self, fp):
        return {"ftbfs.series": "stonking"} if fp == "fp1" else {}

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
    assert "$unshare_mmdebstrap_max_age = -1;" in script
    # No chroot of sbuild's own making when the tarball is missing.
    assert "$unshare_mmdebstrap_auto_create = 0;" in script
    # Build-dependencies come from -proposed when it has them, as on
    # Launchpad; its Release file alone (NotAutomatic) would rank it
    # below the release pocket.
    hook = ("Package: *\\nPin: release a=stonking-proposed\\n"
            "Pin-Priority: 500\\n")
    assert hook in script
    assert "/etc/apt/preferences.d/" in script
    assert script.count("--setup-hook=") == 2  # one per tarball


def test_missing_chroot_is_a_builder_problem(env, monkeypatch):
    tmp, dsc = env
    monkeypatch.setenv("MISSING_CHROOT", "1")
    with pytest.raises(BuilderUnavailable,
                       match="no chroot for d-proposed-amd64.*"
                             "ftbfs builders image"):
        go(builder(tmp), dsc, tmp / "out")


def test_status_tells_the_image_series(env):
    tmp, _ = env
    st = builder(tmp).status()
    assert st["image"] == "fp1" and st["series"] == "stonking"
    fake = FakeLxd("r", tmp / "hosts", image="fp0")  # an older image
    assert builder(tmp, fake).status()["series"] is None


def test_image_series_and_the_snapshot():
    assert series_warning("tenacious", "tenacious") is None
    assert series_warning("stonking", "tenacious") == (
        "image is for stonking, the latest snapshot is tenacious:"
        " run `ftbfs builders image`")
    assert series_warning(None, "tenacious") == (
        "image series unknown (built before ftbfs recorded it):"
        " run `ftbfs builders image`")
    assert series_warning("tenacious", None) is None


class ImageHost(Lxd):
    """An LXD host's images and aliases behind the real `run`
    arguments. Like `lxc`, a command that names no remote for its
    target goes to the local LXD, which is out of reach here (as on
    the server, where the ftbfs user is not in the lxd group)."""

    def __init__(self, remote, aliases=None, fail=()):
        super().__init__(remote)
        self.aliases: dict[str, str] = dict(aliases or {})
        self.images: set[str] = set(self.aliases.values())
        self.fail = fail  # subcommands that fail, e.g. a full disk
        self.published = 0
        self.properties: dict[str, dict[str, str]] = {}

    def instance(self, name):
        return None

    def launch(self, image, name, config):
        pass

    def sh(self, name, script, *, user=False, timeout=600):
        return ""

    def delete(self, name):
        pass

    def _mine(self, ref):
        if not ref.startswith(f"{self.remote}:"):
            raise LxdError('LXD unix socket "/var/snap/lxd/common/lxd/'
                           'unix.socket" not accessible: permission'
                           ' denied')
        return ref.split(":", 1)[1]

    def run(self, *args, timeout=600):
        if args[0] in self.fail or args[:2] in self.fail:
            raise LxdError(f"lxc {args[0]}: failed")
        match args:
            case ("query", ref):
                path = self._mine(ref)
                alias = path.removeprefix("/1.0/images/aliases/")
                if alias in self.aliases:
                    return json.dumps({"target": self.aliases[alias]})
                fp = path.removeprefix("/1.0/images/")
                if fp in self.images:
                    return json.dumps({"fingerprint": fp})
                raise LxdError("Error: Not Found")
            case ("query", "-X", "PATCH", *_):
                # LXD 5.0 never answers it; lxc gives up after 20s.
                raise LxdError("Error: context deadline exceeded")
            case ("query", "-X", "PUT", "--data", data, ref):
                alias = self._mine(ref).removeprefix(
                    "/1.0/images/aliases/")
                assert alias in self.aliases
                assert set(json.loads(data)) == {"target", "description"}
                self.aliases[alias] = json.loads(data)["target"]
            case ("stop", ref):
                self._mine(ref)
            case ("publish", ref, *rest):
                self._mine(ref)
                dest = [a for a in rest if a.endswith(":")]
                self._mine(dest[0] if dest else "local:")
                self.published += 1
                fp = f"new{self.published}"
                self.images.add(fp)
                self.properties[fp] = dict(
                    a.split("=", 1) for a in rest if "=" in a)
                if "--alias" in rest:
                    alias = rest[rest.index("--alias") + 1]
                    assert alias not in self.aliases
                    self.aliases[alias] = fp
            case ("image", "alias", "create", ref, fp):
                alias = self._mine(ref)
                assert alias not in self.aliases and fp in self.images
                self.aliases[alias] = fp
            case ("image", "alias", "delete", ref):
                del self.aliases[self._mine(ref)]
            case ("image", "delete", ref):
                fp = self._mine(ref)
                assert fp not in self.aliases.values()
                self.images.discard(fp)
            case _:
                raise AssertionError(f"unexpected lxc {args}")
        return ""


class Hosts:
    """LXD hosts as this machine's `lxc` sees them. A server-to-server
    `lxc image copy` fails as it does on a server that can use image
    registries; export writes a split image (metadata, root) and
    import reads it back under the same fingerprint."""

    def __init__(self, *hosts):
        self.by_remote = {h.remote: h for h in hosts}
        self.tmp_dirs: list[Path] = []
        for h in hosts:
            h.run = self._run(h, h.run)

    def _run(self, host, run):
        def wrapped(*args, timeout=600):
            match args:
                case ("image", "copy", *_):
                    raise LxdError("lxc image copy: Error: Failed"
                                   " remote image download: Only pull"
                                   " mode is supported for image"
                                   " registries")
                case ("image", "export", ref, target):
                    if "export" in host.fail:
                        raise LxdError("lxc image export: failed")
                    fp = host._mine(ref)
                    assert fp in host.images
                    target = Path(target)
                    self.tmp_dirs.append(target.parent)
                    # As lxc 6.10 names them, metadata first.
                    Path(f"{target}.root").write_text(f"root {fp}")
                    target.write_text(f"meta {fp}")
                case ("image", "import", meta, root, ref):
                    if "import" in host.fail:
                        raise LxdError("lxc image import: failed")
                    assert ref == f"{host.remote}:"
                    fp = Path(meta).read_text().removeprefix("meta ")
                    assert Path(root).read_text() == f"root {fp}"
                    assert fp not in host.images
                    host.images.add(fp)
                case _:
                    return run(*args, timeout=timeout)
            return ""
        return wrapped


def test_build_image_publishes_on_the_builder_host():
    host = ImageHost("laptop", {"ftbfs-builder": "old"})
    fp = build_image(host, "stonking", ["amd64"], log=lambda m: None)
    assert fp == "new1"
    assert host.properties["new1"]["ftbfs.series"] == "stonking"
    assert host.aliases == {"ftbfs-builder": "new1"}
    assert host.images == {"new1"}


def test_build_image_first_time():
    host = ImageHost("laptop")
    assert build_image(host, "stonking", ["amd64"],
                       log=lambda m: None) == "new1"
    assert host.aliases == {"ftbfs-builder": "new1"}


def test_failed_publish_keeps_the_old_image():
    host = ImageHost("laptop", {"ftbfs-builder": "old"},
                     fail=("publish",))
    with pytest.raises(LxdError):
        build_image(host, "stonking", ["amd64"], log=lambda m: None)
    assert host.aliases == {"ftbfs-builder": "old"}
    assert host.images == {"old"}


def test_build_image_after_an_interrupted_one():
    host = ImageHost("laptop", {"ftbfs-builder": "old",
                                "ftbfs-builder-new": "stale"})
    build_image(host, "stonking", ["amd64"], log=lambda m: None)
    assert host.aliases == {"ftbfs-builder": "new1"}


def test_copy_image_moves_the_alias_after_the_copy():
    src = ImageHost("laptop", {"ftbfs-builder": "new"})
    dst = ImageHost("marsangle", {"ftbfs-builder": "old"})
    Hosts(src, dst)
    assert copy_image(src, dst) == "new"
    assert dst.aliases == {"ftbfs-builder": "new"}
    assert dst.images == {"new"}


@pytest.mark.parametrize("step", ["export", "import"])
def test_failed_copy_keeps_the_old_image(step, tmp_path):
    src = ImageHost("laptop", {"ftbfs-builder": "new"})
    dst = ImageHost("marsangle", {"ftbfs-builder": "old"})
    (src if step == "export" else dst).fail = (step,)
    Hosts(src, dst)
    with pytest.raises(LxdError):
        copy_image(src, dst, tmp_dir=tmp_path)
    assert dst.aliases == {"ftbfs-builder": "old"}
    # The temporary copy is gone, even on failure.
    assert list(tmp_path.iterdir()) == []


def test_copy_image_goes_through_a_temporary_directory(tmp_path):
    src = ImageHost("laptop", {"ftbfs-builder": "new"})
    dst = ImageHost("marsangle", {"ftbfs-builder": "old"})
    hosts = Hosts(src, dst)
    copy_image(src, dst, tmp_dir=tmp_path / "cache")
    assert [d.parent for d in hosts.tmp_dirs] == [tmp_path / "cache"]
    assert list((tmp_path / "cache").iterdir()) == []


def test_import_that_gives_another_image_moves_no_alias():
    src = ImageHost("laptop", {"ftbfs-builder": "new"})
    dst = ImageHost("marsangle", {"ftbfs-builder": "old"})
    Hosts(src, dst)
    run = dst.run

    def other(*args, timeout=600):
        if args[:2] == ("image", "import"):
            dst.images.add("other")
            return ""
        return run(*args, timeout=timeout)
    dst.run = other
    with pytest.raises(LxdError, match="not there after its import"):
        copy_image(src, dst)
    assert dst.aliases == {"ftbfs-builder": "old"}


def test_copy_image_needs_room_for_the_image(monkeypatch, tmp_path):
    src = ImageHost("laptop", {"ftbfs-builder": "new"})
    dst = ImageHost("marsangle", {"ftbfs-builder": "old"})
    Hosts(src, dst)
    query = src.query
    monkeypatch.setattr(src, "query", lambda path: dict(
        query(path) or {}, size=550 << 20))
    usage = shutil.disk_usage(tmp_path)
    monkeypatch.setattr(shutil, "disk_usage",
                        lambda p: usage._replace(free=100 << 20))
    with pytest.raises(LxdError, match="needs 550 MB .* 100 MB free"):
        copy_image(src, dst, tmp_dir=tmp_path)
    assert dst.aliases == {"ftbfs-builder": "old"}
    assert list(tmp_path.iterdir()) == []


def test_copy_image_when_the_image_is_already_there():
    src = ImageHost("laptop", {"ftbfs-builder": "new"})
    dst = ImageHost("marsangle", {"ftbfs-builder": "old"})
    dst.images.add("new")
    Hosts(src, dst)
    copy_image(src, dst)
    assert dst.aliases == {"ftbfs-builder": "new"}


class Recorder:
    """subprocess.run for the real `Lxd`: records the argv, answers."""

    def __init__(self, out="{}"):
        self.out = out
        self.argvs: list[list[str]] = []

    def __call__(self, argv, **kw):
        self.argvs.append(argv)
        return subprocess.CompletedProcess(argv, 0, self.out, "")


def test_project_goes_on_every_command(monkeypatch):
    rec = Recorder('{"target": "fp1"}')
    monkeypatch.setattr(subprocess, "run", rec)
    lxd = Lxd("host", "ftbfs")
    lxd.restart("w")
    lxd.sh("w", "true", user=True)
    assert lxd.image("ftbfs-builder") == "fp1"
    lxd.set_alias("ftbfs-builder", "fp2")
    assert rec.argvs[0] == ["lxc", "--project", "ftbfs", "restart",
                            "--force", "host:w"]
    assert rec.argvs[1][:5] == ["lxc", "--project", "ftbfs", "exec",
                                "host:w"]
    # `lxc query` refuses --project: the path carries it.
    assert rec.argvs[2] == [
        "lxc", "query",
        "host:/1.0/images/aliases/ftbfs-builder?project=ftbfs"]
    assert rec.argvs[-1][:4] == ["lxc", "query", "-X", "PUT"]
    assert rec.argvs[-1][-1] == \
        "host:/1.0/images/aliases/ftbfs-builder?project=ftbfs"
    assert lxd.exec_argv("w", ["id"], user=False) == [
        "lxc", "--project", "ftbfs", "exec", "host:w", "--", "id"]


def test_no_project_keeps_the_remote_default(monkeypatch):
    rec = Recorder()
    monkeypatch.setattr(subprocess, "run", rec)
    lxd = Lxd("host")
    lxd.restart("w")
    lxd.query("/1.0/instances/w")
    assert rec.argvs == [["lxc", "restart", "--force", "host:w"],
                         ["lxc", "query", "host:/1.0/instances/w"]]


def test_errors_name_the_subcommand(monkeypatch):
    def fail(argv, **kw):
        return subprocess.CompletedProcess(argv, 1, "", "Error: nope")
    monkeypatch.setattr(subprocess, "run", fail)
    with pytest.raises(LxdError, match=r"^lxc restart --force: Error"):
        Lxd("host", "ftbfs").restart("w")


def test_copy_image_between_projects(monkeypatch, tmp_path):
    """Each side's project goes on its own command."""
    class Exporting(Recorder):
        def __call__(self, argv, **kw):
            if argv[3:5] == ["image", "export"]:
                Path(argv[-1]).write_text("")
                Path(argv[-1] + ".root").write_text("")
            return super().__call__(argv, **kw)
    rec = Exporting()
    monkeypatch.setattr(subprocess, "run", rec)
    src, dst = Lxd("laptop", "ftbfs"), Lxd("marsangle", "ftbfs-b")
    monkeypatch.setattr(src, "image", lambda alias: "fp1")
    monkeypatch.setattr(dst, "image", lambda alias: None)
    present = iter([False, True])
    monkeypatch.setattr(dst, "has_image", lambda fp: next(present))
    monkeypatch.setattr(dst, "set_alias", lambda alias, fp: None)
    copy_image(src, dst, tmp_dir=tmp_path)
    export, imp = (a for a in rec.argvs if a[3:4] == ["image"])
    tmp = Path(export[-1]).parent
    assert tmp.parent == tmp_path
    assert export == ["lxc", "--project", "ftbfs", "image", "export",
                      "laptop:fp1", f"{tmp}/image"]
    assert imp == ["lxc", "--project", "ftbfs-b", "image", "import",
                   f"{tmp}/image", f"{tmp}/image.root", "marsangle:"]
