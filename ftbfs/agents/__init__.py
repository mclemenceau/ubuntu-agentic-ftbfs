# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Agent backends, looked up by name from config."""

from __future__ import annotations

from .base import AgentBackend
from .claude import ClaudeBackend
from .fake import FakeBackend
from .opencode import OpencodeBackend

BACKENDS: dict[str, type[AgentBackend]] = {
    ClaudeBackend.name: ClaudeBackend,
    FakeBackend.name: FakeBackend,
    OpencodeBackend.name: OpencodeBackend,
}


def make_backend(name: str, conf: dict) -> AgentBackend:
    try:
        cls = BACKENDS[name]
    except KeyError:
        raise ValueError(
            f"unknown agent backend {name!r}; known: {sorted(BACKENDS)}"
        ) from None
    conf = dict(conf)
    return cls(tiers=conf.pop("tiers", None), **conf)
