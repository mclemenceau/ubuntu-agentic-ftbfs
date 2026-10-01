"""config.toml: paths, default filter and profiles, backends,
concurrency, builders, identity.

`config.toml` holds the project defaults and is tracked. The site's own
settings (builders, key files, concurrency, changelog identity) go in
the git-ignored `config.local.toml` next to it, merged over the
defaults: tables merge key by key, anything else (scalars, lists) is
replaced.
"""

from __future__ import annotations

import tomllib
from dataclasses import dataclass, field
from pathlib import Path

from .filters import Filter


@dataclass
class Config:
    root: Path
    source_url: str = "http://qa.ubuntuwire.com/ftbfs/"
    state_dir: Path = Path("state")
    cache_dir: Path = Path("cache")
    work_dir: Path = Path("work")
    plugins_dir: Path = Path("plugins")
    pipeline_path: Path = Path("pipeline.toml")
    filter: dict = field(default_factory=dict)
    profiles: dict[str, dict] = field(default_factory=dict)
    concurrency: dict[str, int] = field(default_factory=dict)
    builders: dict[str, dict] = field(default_factory=dict)
    default_backend: str = "claude"
    backends: dict[str, dict] = field(default_factory=dict)
    identity: dict[str, str] = field(default_factory=dict)

    @property
    def db_path(self) -> Path:
        return self.state_dir / "ftbfs.db"

    @property
    def snapshots_dir(self) -> Path:
        return self.state_dir / "snapshots"

    def make_filter(self, profile: str | None = None,
                    **overrides) -> Filter:
        base = dict(self.filter)
        if profile:
            try:
                base.update(self.profiles[profile])
            except KeyError:
                raise ValueError(
                    f"unknown profile {profile!r}; known: "
                    f"{sorted(self.profiles)}"
                ) from None
        base.update({k: v for k, v in overrides.items() if v is not None})
        return Filter.from_dict(base)


LOCAL = "config.local.toml"


def merge(base: dict, over: dict) -> dict:
    """`over` merged into a copy of `base`: tables merge recursively,
    other values replace."""
    out = dict(base)
    for key, value in over.items():
        if isinstance(value, dict) and isinstance(out.get(key), dict):
            out[key] = merge(out[key], value)
        else:
            out[key] = value
    return out


def _read(path: Path) -> dict:
    return tomllib.loads(path.read_text()) if path.exists() else {}


def load_config(root: Path) -> Config:
    raw = merge(_read(root / "config.toml"), _read(root / LOCAL))
    paths = raw.get("paths", {})
    filt = dict(raw.get("filter", {}))
    profiles = filt.pop("profiles", {})
    agents = raw.get("agents", {})

    def p(key: str, default: str) -> Path:
        return root / paths.get(key, default)

    return Config(
        root=root,
        source_url=raw.get("source_url", Config.source_url),
        state_dir=p("state", "state"),
        cache_dir=p("cache", "cache"),
        work_dir=p("work", "work"),
        plugins_dir=p("plugins", "plugins"),
        pipeline_path=p("pipeline", "pipeline.toml"),
        filter=filt,
        profiles=profiles,
        concurrency=raw.get("concurrency", {}),
        builders=raw.get("builders", {}),
        default_backend=agents.get("default_backend", "claude"),
        backends=raw.get("backend", {}),
        identity=raw.get("identity", {}),
    )
