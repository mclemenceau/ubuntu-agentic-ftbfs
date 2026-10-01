# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Local builds with sbuild (unshare mode, the user's sbuild config).

The chroot for `<series>-proposed` is used, matching Launchpad where
development-series builds resolve dependencies against -proposed.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from contextlib import suppress
from dataclasses import dataclass
from pathlib import Path

from .base import BuildResult

ARTIFACT_SUFFIXES = (".deb", ".ddeb", ".udeb", ".changes", ".buildinfo")


POLL_S = 5.0
# Launchpad itself gives up after 150 minutes without output; half an
# hour of silence is already a hang for nearly every package.
IDLE_TIMEOUT = 30 * 60
# On SIGTERM sbuild stops the build and removes its unshare chroot
# itself, in seconds; whatever is left after this gets SIGKILL.
GRACE_S = 60
# The first "Unpacking" line of an unshare build: the chroot directory
# (`$unshare_tmpdir_template`, /tmp/tmp.sbuild.XXXXXXXXXX by default).
CHROOT_RE = re.compile(r"^I: Unpacking \S+ to (/\S*/tmp\.sbuild\.\w+)\.\.\.$",
                       re.MULTILINE)


def fetch_source(source: str, version: str, cache_dir: Path) -> Path:
    """Download (once) the source package; return the .dsc path."""
    dest = cache_dir / source / version
    dsc = next(dest.glob("*.dsc"), None) if dest.exists() else None
    if dsc:
        return dsc
    dest.mkdir(parents=True, exist_ok=True)
    proc = subprocess.run(
        ["pull-lp-source", "--download-only", source, version],
        cwd=dest, capture_output=True, text=True, timeout=900,
    )
    dsc = next(dest.glob("*.dsc"), None)
    if proc.returncode != 0 or dsc is None:
        raise RuntimeError(
            f"pull-lp-source {source} {version} failed: "
            f"{(proc.stderr or proc.stdout).strip()[-500:]}"
        )
    return dsc


def sbuild_command(dsc: Path | str, arch: str, dist: str,
                   extra_repos: list[str]) -> list[str]:
    cmd = ["sbuild", f"--dist={dist}", f"--arch={arch}",
           "--no-run-lintian", "--no-run-autopkgtest", "--no-run-piuparts",
           "--no-clean-source"]
    for repo in extra_repos:
        cmd.append(f"--extra-repository={repo}")
    cmd.append(str(dsc))
    return cmd


def _output_size(build_dir: Path) -> int:
    return sum(p.stat().st_size for p in (*build_dir.glob("*.build"),
                                          build_dir / "sbuild.out")
               if p.exists() and not p.is_symlink())


def _start_time(pid: int) -> int | None:
    """Field 22 of /proc/<pid>/stat (start time in ticks since boot),
    which tells a process from a later one that reuses its pid."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text()
    except OSError:
        return None
    # Fields count after the parenthesised command name, which may
    # itself contain spaces or parentheses.
    return int(stat.rsplit(")", 1)[1].split()[19])


def descendants(roots: list[int]) -> dict[int, int]:
    """Every live descendant of `roots`, as {pid: start time}. Processes
    that left the roots' session or process group (`setsid`) are still
    found, as long as their parents are alive."""
    children: dict[int, list[int]] = {}
    for d in Path("/proc").iterdir():
        if not d.name.isdigit():
            continue
        try:
            stat = (d / "stat").read_text()
        except OSError:
            continue
        ppid = int(stat.rsplit(")", 1)[1].split()[1])
        children.setdefault(ppid, []).append(int(d.name))
    found: dict[int, int] = {}
    todo = list(roots)
    while todo:
        for pid in children.get(todo.pop(), []):
            if pid not in found and (st := _start_time(pid)) is not None:
                found[pid] = st
                todo.append(pid)
    return found


def stop(proc: subprocess.Popen, grace: float) -> None:
    """Stop a command started with start_new_session, and everything it
    started. sbuild-usernsexec calls setsid, so the build itself is in
    another session: killing the command's process group alone leaves
    the build running forever, an orphan. So: SIGTERM the group, which
    lets sbuild stop the build and clean up; after `grace` seconds,
    SIGKILL every descendant recorded before and after the SIGTERM that
    is still alive (same pid and start time)."""
    tree = descendants([proc.pid])
    with suppress(ProcessLookupError):
        os.killpg(proc.pid, signal.SIGTERM)
    with suppress(subprocess.TimeoutExpired):
        proc.wait(timeout=grace)
    tree |= descendants([proc.pid, *tree])
    if proc.poll() is None:  # once reaped, its pid may be reused
        os.killpg(proc.pid, signal.SIGKILL)
    for pid, started in tree.items():
        if _start_time(pid) == started:
            with suppress(ProcessLookupError):
                os.kill(pid, signal.SIGKILL)
    proc.wait()


def remove_chroot(build_dir: Path) -> Path | None:
    """Remove the unshare chroot a killed sbuild left behind, named in
    its log; return it if it was there. Its files belong to subordinate
    uids, so rm runs as root of a user namespace mapping them."""
    for log in sorted(build_dir.glob("*.build")):
        if log.is_symlink():
            continue
        with log.open(errors="replace") as f:
            # The line comes before the package's own output starts.
            head = f.read(64_000)
        m = CHROOT_RE.search(head)
        chroot = Path(m.group(1)) if m else None
        if chroot is None or chroot.is_symlink() or not chroot.is_dir():
            continue
        try:
            shutil.rmtree(chroot)
        except PermissionError:
            subprocess.run(["unshare", "--map-auto", "--map-root-user",
                            "rm", "-rf", "--", str(chroot)],
                           capture_output=True, timeout=600)
        return chroot
    return None


@dataclass
class Supervised:
    returncode: int
    duration_s: float
    timed_out: bool
    stalled: bool


def supervise(cmd: list[str], build_dir: Path, env: dict[str, str],
              timeout: int, idle_timeout: int,
              on_kill: Callable[[], None] | None = None,
              grace: float | None = None) -> Supervised:
    """Run a build command with its output in build_dir/sbuild.out.
    It is killed after `timeout` seconds, or once its output (sbuild.out
    and any .build log in build_dir) has not grown for `idle_timeout`
    seconds: a hung test suite would otherwise hold a build slot for the
    whole timeout. It gets SIGTERM and `grace` seconds to clean up, then
    it and everything it started get SIGKILL (see stop()). `on_kill`
    then stops or cleans whatever the command left elsewhere (e.g. on a
    remote host)."""
    build_dir.mkdir(parents=True, exist_ok=True)
    (build_dir / "command.txt").write_text(" ".join(cmd) + "\n")
    start = time.monotonic()
    with (build_dir / "sbuild.out").open("w") as out:
        proc = subprocess.Popen(cmd, cwd=build_dir, stdout=out,
                                stderr=subprocess.STDOUT, env=env,
                                start_new_session=True)
        (build_dir / "pid").write_text(str(proc.pid))
        timed_out = stalled = False
        size, grew = -1, start
        while True:
            try:
                proc.wait(timeout=POLL_S)
                break
            except subprocess.TimeoutExpired:
                pass
            t = time.monotonic()
            if (new := _output_size(build_dir)) != size:
                size, grew = new, t
            stalled = t - grew >= idle_timeout
            if stalled or t - start >= timeout:
                timed_out = True
                stop(proc, GRACE_S if grace is None else grace)
                if on_kill is not None:
                    on_kill()
                break
    return Supervised(proc.returncode, time.monotonic() - start,
                      timed_out, stalled)


def build(dsc: Path, arch: str, dist: str, build_dir: Path,
          extra_repos: list[str] | None = None, parallel: int = 8,
          timeout: int = 4 * 3600,
          idle_timeout: int = IDLE_TIMEOUT) -> BuildResult:
    """sbuild the .dsc on this machine (see supervise() for timeouts)."""
    cmd = sbuild_command(dsc, arch, dist, extra_repos or [])
    env = {**os.environ, "DEB_BUILD_OPTIONS": f"parallel={parallel}"}
    run = supervise(cmd, build_dir, env, timeout, idle_timeout,
                    on_kill=lambda: remove_chroot(build_dir))
    logs = sorted(p for p in build_dir.glob("*.build") if not p.is_symlink())
    for p in build_dir.iterdir():
        if p.suffix in ARTIFACT_SUFFIXES:
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    return BuildResult(
        ok=run.returncode == 0 and not run.timed_out,
        exit_code=run.returncode,
        log=logs[-1] if logs else None,
        duration_s=run.duration_s,
        timed_out=run.timed_out,
        stalled=run.stalled,
    )


@dataclass
class LocalBuilder:
    """sbuild on this machine, with the user's sbuild config."""

    name: str
    slots: int
    arches: tuple[str, ...] = ("amd64",)
    parallel: int | None = None

    def build(self, dsc: Path, arch: str, dist: str, build_dir: Path,
              extra_repos: list[str], parallel: int, timeout: int,
              idle_timeout: int) -> BuildResult:
        return build(dsc, arch, dist, build_dir, extra_repos,
                     parallel=parallel, timeout=timeout,
                     idle_timeout=idle_timeout)
