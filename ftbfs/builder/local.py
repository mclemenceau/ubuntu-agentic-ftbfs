"""Local builds with sbuild (unshare mode, the user's sbuild config).

The chroot for `<series>-proposed` is used, matching Launchpad where
development-series builds resolve dependencies against -proposed.
"""

from __future__ import annotations

import os
import shutil
import signal
import subprocess
import time
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from .base import BuildResult

ARTIFACT_SUFFIXES = (".deb", ".ddeb", ".udeb", ".changes", ".buildinfo")


POLL_S = 5.0
# Launchpad itself gives up after 150 minutes without output; half an
# hour of silence is already a hang for nearly every package.
IDLE_TIMEOUT = 30 * 60


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


@dataclass
class Supervised:
    returncode: int
    duration_s: float
    timed_out: bool
    stalled: bool


def supervise(cmd: list[str], build_dir: Path, env: dict[str, str],
              timeout: int, idle_timeout: int,
              on_kill: Callable[[], None] | None = None) -> Supervised:
    """Run a build command with its output in build_dir/sbuild.out.
    It is killed after `timeout` seconds, or once its output (sbuild.out
    and any .build log in build_dir) has not grown for `idle_timeout`
    seconds: a hung test suite would otherwise hold a build slot for the
    whole timeout. `on_kill` then stops whatever the command left
    running elsewhere (e.g. on a remote host)."""
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
                os.killpg(proc.pid, signal.SIGKILL)
                proc.wait()
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
    run = supervise(cmd, build_dir, env, timeout, idle_timeout)
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
