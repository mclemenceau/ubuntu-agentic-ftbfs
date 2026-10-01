# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""config.toml defaults with the site's config.local.toml merged over."""

from pathlib import Path

import pytest

from ftbfs.config import LOOPBACK_HOSTS, load_config, merge

ROOT = Path(__file__).parent.parent


def test_merge_tables_merge_and_values_replace():
    base = {"a": {"x": 1, "y": [1, 2], "t": {"k": 1}}, "s": "base"}
    over = {"a": {"y": [3], "t": {"j": 2}, "z": 0}, "s": "local"}
    assert merge(base, over) == {
        "a": {"x": 1, "y": [3], "t": {"k": 1, "j": 2}, "z": 0},
        "s": "local"}
    assert base["a"]["t"] == {"k": 1}  # inputs untouched


def test_local_config_overrides_project_defaults(tmp_path):
    (tmp_path / "config.toml").write_text(
        '[concurrency]\ndeterministic = 8\nbuild = 2\n'
        '[filter]\ncomponents = ["universe"]\nstates = ["F"]\n'
        '[filter.profiles.main]\ncomponents = ["main"]\n'
        '[backend.opencode.tiers]\nsmall = "s"\nmedium = "m"\n')
    (tmp_path / "config.local.toml").write_text(
        '[concurrency]\nbuild = 4\n'
        '[filter]\nstates = ["F", "M"]\n'
        '[backend.opencode]\napi_keys = { openrouter = "/k" }\n'
        '[builders.local]\nkind = "lxd"\nslots = 2\n'
        '[identity]\nname = "A B"\nemail = "a@example.org"\n')
    c = load_config(tmp_path)
    assert c.concurrency == {"deterministic": 8, "build": 4}
    assert c.filter == {"components": ["universe"], "states": ["F", "M"]}
    assert c.profiles == {"main": {"components": ["main"]}}
    assert c.backends == {"opencode": {
        "tiers": {"small": "s", "medium": "m"},
        "api_keys": {"openrouter": "/k"}}}
    assert c.builders == {"local": {"kind": "lxd", "slots": 2}}
    assert c.identity == {"name": "A B", "email": "a@example.org"}


def test_without_local_config(tmp_path):
    (tmp_path / "config.toml").write_text("[concurrency]\nbuild = 2\n")
    c = load_config(tmp_path)
    assert c.builders == {} and c.identity == {}


def test_tracked_config_is_site_neutral():
    """Site settings belong in config.local.toml, not in config.toml."""
    import tomllib

    raw = tomllib.loads((ROOT / "config.toml").read_text())
    assert "builders" not in raw and "identity" not in raw
    assert "api_keys" not in raw.get("backend", {}).get("opencode", {})


def test_example_local_config_loads(tmp_path):
    (tmp_path / "config.toml").write_text(
        (ROOT / "config.toml").read_text())
    (tmp_path / "config.local.toml").write_text(
        (ROOT / "config.local.toml.example").read_text())
    c = load_config(tmp_path)
    assert set(c.builders) == {"local", "buildhost"}
    assert c.identity["name"] and c.identity["email"]


def test_web_allowed_hosts(tmp_path):
    assert load_config(tmp_path).allowed_hosts == list(LOOPBACK_HOSTS)
    (tmp_path / "config.toml").write_text(
        (ROOT / "config.toml").read_text())
    assert load_config(tmp_path).allowed_hosts == list(LOOPBACK_HOSTS)
    (tmp_path / "config.local.toml").write_text(
        '[web]\nallowed_hosts = ["ftbfs.example.org"]\n')
    assert load_config(tmp_path).allowed_hosts == ["ftbfs.example.org"]
    (tmp_path / "config.local.toml").write_text(
        '[web]\nallowed_hosts = "ftbfs.example.org"\n')
    with pytest.raises(ValueError, match="allowed_hosts"):
        load_config(tmp_path)
