"""Agent backends, looked up by name from config."""

from __future__ import annotations

from .base import AgentBackend
from .fake import FakeBackend

BACKENDS: dict[str, type[AgentBackend]] = {
    FakeBackend.name: FakeBackend,
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
