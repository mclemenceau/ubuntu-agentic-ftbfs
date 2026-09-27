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
from dataclasses import dataclass
from pathlib import Path

ARTIFACT_SUFFIXES = (".deb", ".ddeb", ".udeb", ".changes", ".buildinfo")


@dataclass
class LocalBuild:
    ok: bool  # dpkg-buildpackage succeeded
    exit_code: int
    log: Path | None
    duration_s: float
    timed_out: bool = False


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


def sbuild_command(dsc: Path, arch: str, dist: str,
                   extra_repos: list[str]) -> list[str]:
    cmd = ["sbuild", f"--dist={dist}", f"--arch={arch}",
           "--no-run-lintian", "--no-run-autopkgtest", "--no-run-piuparts",
           "--no-clean-source"]
    for repo in extra_repos:
        cmd.append(f"--extra-repository={repo}")
    cmd.append(str(dsc))
    return cmd


def build(dsc: Path, arch: str, dist: str, build_dir: Path,
          extra_repos: list[str] | None = None, parallel: int = 8,
          timeout: int = 4 * 3600) -> LocalBuild:
    build_dir.mkdir(parents=True, exist_ok=True)
    cmd = sbuild_command(dsc, arch, dist, extra_repos or [])
    (build_dir / "command.txt").write_text(" ".join(cmd) + "\n")
    env = {**os.environ, "DEB_BUILD_OPTIONS": f"parallel={parallel}"}
    start = time.monotonic()
    with (build_dir / "sbuild.out").open("w") as out:
        proc = subprocess.Popen(cmd, cwd=build_dir, stdout=out,
                                stderr=subprocess.STDOUT, env=env,
                                start_new_session=True)
        (build_dir / "pid").write_text(str(proc.pid))
        timed_out = False
        try:
            proc.wait(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            os.killpg(proc.pid, signal.SIGKILL)
            proc.wait()
    duration = time.monotonic() - start
    logs = sorted(p for p in build_dir.glob("*.build") if not p.is_symlink())
    for p in build_dir.iterdir():
        if p.suffix in ARTIFACT_SUFFIXES:
            p.unlink()
        elif p.is_dir():
            shutil.rmtree(p, ignore_errors=True)
    return LocalBuild(
        ok=proc.returncode == 0 and not timed_out,
        exit_code=proc.returncode,
        log=logs[-1] if logs else None,
        duration_s=duration,
        timed_out=timed_out,
    )
