# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Builds in LXD containers, on this machine or on a remote LXD host.

Each slot of an LXD builder is a long-lived worker container,
`ftbfs-<builder>-<n>`, launched from the `ftbfs-builder` image. The
image is built once (`ftbfs builders image`) and copied to every host,
so all hosts build with the same sbuild and the same chroot tarball.
Every build still gets a fresh chroot: sbuild unpacks the tarball for
each build (unshare mode), so a worker carries nothing over.

A build pushes the source package into the worker, runs sbuild there
with its log streamed back into the local build dir (so the stall
watcher works as for local builds) and pulls the .build log. A worker
is recreated when the image changes, and force-restarted the first
time this process uses it and after a killed build, which stops
anything a crash or kill left running in it.

Everything goes through the `lxc` CLI, with its remotes and their TLS
trust as configured by `lxc remote add`.
"""

from __future__ import annotations

import contextlib
import fcntl
import json
import os
import re
import shlex
import subprocess
import threading
import time
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path

from .base import BuilderUnavailable, BuildResult
from .local import sbuild_command, supervise

IMAGE_ALIAS = "ftbfs-builder"
BASE_IMAGE = "ubuntu:26.04"
UID = 2000  # the image's `builder` user (1000 is the image's `ubuntu`)
HOME = "/home/builder"
BUILD_DIR = f"{HOME}/build"
READY_TIMEOUT_S = 120
WORKER_CONFIG = {"security.nesting": "true"}
NAME_RE = re.compile(r"^[a-z][a-z0-9-]{0,40}$")


class LxdError(BuilderUnavailable):
    """An lxc command failed: host unreachable, instance missing, ..."""


class Lxd:
    """The `lxc` CLI, bound to one remote."""

    def __init__(self, remote: str):
        self.remote = remote

    def ref(self, name: str) -> str:
        return f"{self.remote}:{name}"

    def run(self, *args: str, timeout: int = 600) -> str:
        try:
            proc = subprocess.run(["lxc", *args], capture_output=True,
                                  text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            raise LxdError(f"lxc {args[0]} timed out on {self.remote}"
                           ) from None
        if proc.returncode != 0:
            raise LxdError(f"lxc {' '.join(args[:2])}: "
                           f"{(proc.stderr or proc.stdout).strip()[-500:]}")
        return proc.stdout

    def query(self, path: str) -> dict | None:
        """GET on the LXD API; None when the object does not exist."""
        try:
            return json.loads(self.run("query", f"{self.remote}:{path}",
                                       timeout=60))
        except LxdError as e:
            if "not found" in str(e).lower():
                return None
            raise

    def instance(self, name: str) -> dict | None:
        return self.query(f"/1.0/instances/{name}")

    def image(self, alias: str) -> str | None:
        """Fingerprint of the image behind an alias."""
        found = self.query(f"/1.0/images/aliases/{alias}")
        return found["target"] if found else None

    def launch(self, image: str, name: str, config: dict[str, str]) -> None:
        args = ["launch", f"{self.remote}:{image}"
                if ":" not in image else image, self.ref(name)]
        for k, v in config.items():
            args += ["-c", f"{k}={v}"]
        self.run(*args)

    def delete(self, name: str) -> None:
        self.run("delete", "--force", self.ref(name))

    def restart(self, name: str) -> None:
        self.run("restart", "--force", self.ref(name))

    def start(self, name: str) -> None:
        self.run("start", self.ref(name))

    def exec_argv(self, name: str, argv: list[str], *, user: bool,
                  env: dict[str, str] | None = None,
                  cwd: str | None = None) -> list[str]:
        cmd = ["lxc", "exec", self.ref(name)]
        if user:
            cmd += ["--user", str(UID), "--group", str(UID),
                    "--env", f"HOME={HOME}", "--env", "USER=builder",
                    "--env", "LOGNAME=builder"]
        for k, v in (env or {}).items():
            cmd += ["--env", f"{k}={v}"]
        if cwd:
            cmd += ["--cwd", cwd]
        return [*cmd, "--", *argv]

    def sh(self, name: str, script: str, *, user: bool = False,
           timeout: int = 600) -> str:
        return self.run(*self.exec_argv(name, ["sh", "-ec", script],
                                        user=user)[1:], timeout=timeout)

    def push(self, name: str, files: list[Path], dest_dir: str) -> None:
        self.run("file", "push", "--uid", str(UID), "--gid", str(UID),
                 *map(str, files), f"{self.ref(name)}{dest_dir}/")

    def pull(self, name: str, src: str, dest: Path) -> None:
        self.run("file", "pull", f"{self.ref(name)}{src}", str(dest))


def dsc_files(dsc: Path) -> list[Path]:
    """The .dsc and the files it lists (orig, debian tarball, ...)."""
    names, in_files = [], False
    for line in dsc.read_text(errors="replace").splitlines():
        if line.startswith("Files:"):
            in_files = True
        elif in_files and line.startswith(" "):
            names.append(line.split()[-1])
        elif in_files:
            break
    return [dsc, *(dsc.parent / n for n in names)]


@dataclass
class LxdBuilder:
    name: str
    slots: int
    remote: str
    arches: tuple[str, ...] = ("amd64",)
    parallel: int | None = None
    image: str = IMAGE_ALIAS
    # Per-worker lock files: another ftbfs process (a second run) must
    # neither share a worker nor restart it under a running build.
    lock_dir: Path | None = None
    lxd: Lxd = field(init=False, repr=False)

    def __post_init__(self):
        if not NAME_RE.match(self.name):
            raise ValueError(f"builder name {self.name!r}: use lowercase"
                             " letters, digits and dashes (it names"
                             " containers)")
        self.lxd = Lxd(self.remote)
        self._lock = threading.Lock()
        self._free = [self.worker_name(i) for i in range(self.slots)]
        self._fresh: set[str] = set()  # restarted by this process

    def worker_name(self, i: int) -> str:
        return f"ftbfs-{self.name}-{i + 1}"

    @property
    def workers(self) -> list[str]:
        return [self.worker_name(i) for i in range(self.slots)]

    # -- workers ------------------------------------------------------------

    @contextmanager
    def _worker(self) -> Iterator[str]:
        """A worker for one build. The pool never hands this builder
        more builds than it has slots, so one is always free in this
        process; the lock waits for another process's build on it. The
        kernel drops the lock when its process dies, so holding it
        means nothing live is using the worker."""
        with self._lock:
            w = self._free.pop()
        try:
            if self.lock_dir is None:
                yield w
                return
            self.lock_dir.mkdir(parents=True, exist_ok=True)
            with open(self.lock_dir / f"{w}.lock", "w") as f:
                fcntl.flock(f, fcntl.LOCK_EX)
                yield w
        finally:
            with self._lock:
                self._free.append(w)

    def prepare(self, w: str) -> None:
        """Make the worker current (latest image), fresh and running."""
        fp = self.lxd.image(self.image)
        if fp is None:
            raise LxdError(f"{self.remote}: no {self.image} image; run"
                           " `ftbfs builders image`")
        inst = self.lxd.instance(w)
        if inst is not None and \
                inst["config"].get("volatile.base_image") != fp:
            self.lxd.delete(w)
            inst = None
        if inst is None:
            self.lxd.launch(fp, w, WORKER_CONFIG)
        elif inst["status"] != "Running":
            self.lxd.start(w)
        elif w not in self._fresh:
            self.lxd.restart(w)
        self._fresh.add(w)
        self._wait_ready(w)

    def _wait_ready(self, w: str) -> None:
        """Booted, with DNS: sbuild's first step is apt-get update."""
        deadline = time.monotonic() + READY_TIMEOUT_S
        while True:
            try:
                self.lxd.sh(w, "getent hosts archive.ubuntu.com",
                            timeout=30)
                return
            except LxdError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(1)

    def reset(self, w: str) -> None:
        """Stop everything a killed build left running in the worker."""
        self._fresh.discard(w)
        # On failure, prepare() restarts it before its next build.
        with contextlib.suppress(LxdError):
            self.lxd.restart(w)
            self._fresh.add(w)

    # -- builds -------------------------------------------------------------

    def build(self, dsc: Path, arch: str, dist: str, build_dir: Path,
              extra_repos: list[str], parallel: int, timeout: int,
              idle_timeout: int) -> BuildResult:
        with self._worker() as w:
            self.prepare(w)
            self.lxd.sh(w, f"rm -rf {BUILD_DIR}; mkdir {BUILD_DIR}",
                        user=True)
            self.lxd.push(w, dsc_files(dsc), BUILD_DIR)
            # --verbose: the log also goes to stdout, which is streamed
            # into build_dir/sbuild.out for the stall watcher.
            sbuild = sbuild_command(dsc.name, arch, dist, extra_repos)
            cmd = self.lxd.exec_argv(
                w, [sbuild[0], "--verbose", *sbuild[1:]], user=True,
                env={"DEB_BUILD_OPTIONS": f"parallel={parallel}"},
                cwd=BUILD_DIR)
            build_dir.mkdir(parents=True, exist_ok=True)
            (build_dir / "worker.txt").write_text(f"{self.lxd.ref(w)}\n")
            run = supervise(cmd, build_dir, dict(os.environ), timeout,
                            idle_timeout, on_kill=lambda: self.reset(w))
            log = self._fetch_log(w, build_dir)
        return BuildResult(
            ok=run.returncode == 0 and not run.timed_out,
            exit_code=run.returncode,
            log=log,
            duration_s=run.duration_s,
            timed_out=run.timed_out,
            stalled=run.stalled,
        )

    def _fetch_log(self, w: str, build_dir: Path) -> Path | None:
        """Pull the (non-symlink) .build log; sbuild names it with a
        timestamp and points `<pkg>_<ver>_<arch>.build` at it."""
        try:
            out = self.lxd.sh(
                w, f"find {BUILD_DIR} -maxdepth 1 -type f"
                   " -name '*.build' | sort", user=True, timeout=60)
        except LxdError:
            return None
        logs = out.split()
        if not logs:
            return None
        dest = build_dir / Path(logs[-1]).name
        try:
            self.lxd.pull(w, logs[-1], dest)
        except LxdError:
            return None
        return dest

    # -- status -------------------------------------------------------------

    def status(self) -> dict:
        """Image and workers as the host sees them (for `builders`)."""
        try:
            fp = self.lxd.image(self.image)
            workers = {}
            for w in self.workers:
                inst = self.lxd.instance(w)
                workers[w] = None if inst is None else {
                    "status": inst["status"],
                    "current": inst["config"].get("volatile.base_image")
                    == fp}
        except LxdError as e:
            return {"reachable": False, "error": str(e)}
        return {"reachable": True, "image": fp, "workers": workers}


# -- the image ---------------------------------------------------------------

ARCHIVE = "http://archive.ubuntu.com/ubuntu"

SBUILD_CONFIG = """\
# Written by ftbfs (builder image). Unshare mode: every build unpacks a
# fresh chroot from ~/.cache/sbuild/<dist>-<arch>.tar.
$chroot_mode = 'unshare';
$run_lintian = 0;
$clean_source = 0;
1;
"""


def image_script(series: str, arches: list[str]) -> str:
    """Root shell script that turns a fresh Ubuntu container into the
    builder image."""
    dist = f"{series}-proposed"
    suites = [series, f"{series}-updates", dist]
    mirrors = " ".join(shlex.quote(f"deb {ARCHIVE} {s} main universe")
                       for s in suites)
    tarballs = "\n".join(
        f"runuser -u builder -- mmdebstrap --mode=unshare"
        f" --variant=buildd --arch={a} --include=ca-certificates"
        f" {series} {HOME}/.cache/sbuild/{dist}-{a}.tar {mirrors}"
        for a in arches)
    return f"""\
export DEBIAN_FRONTEND=noninteractive
apt-get update -q
apt-get install -yq --no-install-recommends sbuild mmdebstrap uidmap \\
    ubuntu-keyring ca-certificates
# The sbuild AppArmor profile (from the apparmor package) blocks apt's
# network access inside the unshare chroot when sbuild runs in a
# container. Complain mode, as on the ftbfs host itself; the container
# is the isolation boundary here.
sed -i 's/flags=(/&complain /' /etc/apparmor.d/sbuild
grep -q 'complain' /etc/apparmor.d/sbuild
useradd -m -u {UID} -s /bin/bash builder
runuser -u builder -- mkdir -p {HOME}/.config/sbuild {HOME}/.cache/sbuild
cat > {HOME}/.config/sbuild/config.pl <<'EOF'
{SBUILD_CONFIG}EOF
chown builder: {HOME}/.config/sbuild/config.pl
{tarballs}
apt-get clean
"""


def build_image(lxd: Lxd, series: str, arches: list[str],
                alias: str = IMAGE_ALIAS, log=print) -> str:
    """Build the builder image on `lxd`'s host and publish it under
    `alias` there. Returns its fingerprint."""
    tmp = "ftbfs-image-build"
    if lxd.instance(tmp) is not None:
        lxd.delete(tmp)
    log(f"launching {BASE_IMAGE} as {lxd.ref(tmp)}")
    lxd.launch(BASE_IMAGE, tmp, WORKER_CONFIG)
    try:
        lxd.sh(tmp, "cloud-init status --wait >/dev/null || true",
               timeout=600)
        log(f"installing sbuild, creating {series}-proposed chroots for"
            f" {', '.join(arches)}")
        lxd.sh(tmp, image_script(series, arches), timeout=3600)
        lxd.run("stop", lxd.ref(tmp))
        old = lxd.image(alias)
        if old is not None:
            lxd.run("image", "alias", "delete", lxd.ref(alias))
        date = datetime.now(UTC).strftime("%Y-%m-%d")
        log(f"publishing {lxd.ref(alias)}")
        lxd.run("publish", lxd.ref(tmp), "--alias", alias,
                f"description=ftbfs builder {series} {date}",
                timeout=3600)
        if old is not None:
            _delete_image(lxd, old)
    finally:
        lxd.delete(tmp)
    fp = lxd.image(alias)
    assert fp is not None
    return fp


def copy_image(src: Lxd, dst: Lxd, alias: str = IMAGE_ALIAS) -> str:
    """Make `dst`'s alias point at the same image as `src`'s."""
    fp = src.image(alias)
    if fp is None:
        raise LxdError(f"{src.remote}: no {alias} image")
    old = dst.image(alias)
    if old == fp:
        return fp
    if old is not None:
        dst.run("image", "alias", "delete", dst.ref(alias))
    # Push: the source host sends it, so it need not listen on the
    # network (this machine's LXD usually does not).
    src.run("image", "copy", "--mode=push", src.ref(alias),
            f"{dst.remote}:", "--alias", alias, timeout=3600)
    if old is not None:
        _delete_image(dst, old)
    return fp


def _delete_image(lxd: Lxd, fp: str) -> None:
    """Workers still running from an old image keep working until they
    are recreated; LXD keeps what they need."""
    with contextlib.suppress(LxdError):
        lxd.run("image", "delete", lxd.ref(fp))
