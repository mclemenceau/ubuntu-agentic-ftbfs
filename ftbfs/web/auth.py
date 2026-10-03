# Copyright (C) 2026 Matthieu Clemenceau
# SPDX-License-Identifier: GPL-3.0-or-later

"""Who is asking, and what they may do: Launchpad login and roles.

Reading is public, without costs. Logging in goes through Launchpad's
OAuth 1.0a flow (the one launchpadlib uses), asking only for read access
to public data: the token proves who the person is, then is dropped.
The session is a signed cookie holding their Launchpad name and the
teams of theirs that `[web.roles]` mentions. The role itself is worked
out from the config at each request, so a role taken away in the config
ends when the server restarts with it, not when the session expires.

Without `[web.auth]`, the UI is for whoever sits at the machine: it
serves loopback only, and its user is an operator.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import json
import re
import time
import urllib.error
import uuid
from dataclasses import dataclass, field
from enum import IntEnum
from pathlib import Path
from urllib.parse import parse_qs, urlencode, urlsplit
from urllib.request import Request, urlopen

from ..config import LOOPBACK_HOSTS, Config

LAUNCHPAD = "https://launchpad.net"
LP_API = "https://api.launchpad.net/devel"
# Launchpad person and team names.
LP_NAME = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")
SESSION_COOKIE = "ftbfs_session"
OAUTH_COOKIE = "ftbfs_oauth"
OAUTH_TTL_S = 600


class Role(IntEnum):
    """Each role can do what the ones below it can."""
    ANONYMOUS = 0  # every page, without costs
    VIEWER = 1     # costs
    REVIEWER = 2   # gates, dispositions, retries
    OPERATOR = 3   # start, pause, resume and cancel runs; kill agents

    def __str__(self) -> str:
        return self.name.lower()


@dataclass(frozen=True)
class User:
    name: str | None  # Launchpad name; None when anonymous or local
    role: Role
    local: bool = False  # no [web.auth]: whoever is at the machine

    @property
    def by(self) -> str:
        """The actor recorded on events and gates."""
        return f"web:{self.name}" if self.name else "web"

    @property
    def costs(self) -> bool:
        return self.role >= Role.VIEWER

    def can(self, role: str | Role) -> bool:
        if isinstance(role, str):
            role = Role[role.upper()]
        return self.role >= role


ANONYMOUS = User(None, Role.ANONYMOUS)
LOCAL = User(None, Role.OPERATOR, local=True)


@dataclass
class AuthSettings:
    """`[web]`, `[web.auth]` and `[web.roles]` of the config."""
    public_url: str
    secret: bytes
    roles: dict[Role, set[str]] = field(default_factory=dict)
    consumer_key: str = "ftbfs"
    session_days: float = 30

    @property
    def secure(self) -> bool:
        return self.public_url.startswith("https://")

    @property
    def names(self) -> set[str]:
        """Every person or team some role is granted to."""
        return set().union(*self.roles.values()) if self.roles else set()

    def role_of(self, name: str, teams: set[str]) -> Role:
        mine = {name} | teams
        return max((r for r, names in self.roles.items() if names & mine),
                   default=Role.ANONYMOUS)

    @classmethod
    def from_config(cls, config: Config) -> AuthSettings | None:
        """None when `[web.auth]` is absent. Raises ValueError on a
        partial or invalid setup: half-configured auth must not serve."""
        web = config.web
        auth = web.get("auth")
        if not auth:
            return None
        if auth.get("provider") != "launchpad":
            raise ValueError('[web.auth] provider must be "launchpad"')
        url = web.get("public_url", "")
        parts = urlsplit(url)
        if parts.scheme not in ("http", "https") or not parts.netloc \
                or parts.path not in ("", "/") or parts.query:
            raise ValueError("[web] public_url must be the UI's base URL,"
                             " e.g. https://ftbfs.example.org")
        if parts.hostname not in {h.lower() for h in config.allowed_hosts}:
            raise ValueError(f"[web] public_url's host {parts.hostname!r}"
                             " is not in [web] allowed_hosts")
        if "session_secret_file" not in web:
            raise ValueError("[web] session_secret_file is required with"
                             " [web.auth]")
        roles: dict[Role, set[str]] = {}
        for key, names in web.get("roles", {}).items():
            try:
                role = Role[key.upper()]
            except KeyError:
                raise ValueError(f"[web.roles] {key}: not a role; roles"
                                 " are viewer, reviewer, operator") from None
            if role == Role.ANONYMOUS or not isinstance(names, list):
                raise ValueError(f"[web.roles] {key} must be a list of"
                                 " Launchpad names")
            clean = {str(n).removeprefix("~").lower() for n in names}
            bad = sorted(n for n in clean if not LP_NAME.match(n))
            if bad:
                raise ValueError(f"[web.roles] {key}: not Launchpad"
                                 f" names: {bad}")
            roles[role] = clean
        return cls(
            public_url=url.rstrip("/"),
            secret=read_secret(Path(web["session_secret_file"])
                               .expanduser()),
            roles=roles,
            consumer_key=auth.get("consumer_key", "ftbfs"),
            session_days=float(web.get("session_days", 30)),
        )


def read_secret(path: Path) -> bytes:
    """The session signing key: a private file of at least 32 bytes."""
    try:
        mode = path.stat().st_mode
        data = path.read_bytes().strip()
    except OSError as e:
        raise ValueError(f"cannot read the session secret {path}: {e}."
                         " Create it with: install -m 600 /dev/null"
                         f" {path} && head -c 32 /dev/urandom | base64"
                         f" > {path}") from None
    if mode & 0o077:
        raise ValueError(f"{path} is readable by others; chmod 600 it")
    if len(data) < 32:
        raise ValueError(f"{path} is too short: use at least 32 bytes")
    return data


def check_serving(config: Config, bind: str | None = None) -> None:
    """Fail closed: without `[web.auth]`, the UI must not be reachable
    as anything but loopback, nor bound beyond it."""
    if AuthSettings.from_config(config) is not None:
        return
    remote = [h for h in config.allowed_hosts if h not in LOOPBACK_HOSTS]
    if remote:
        raise ValueError(f"[web] allowed_hosts lists {', '.join(remote)}"
                         " but [web.auth] is not configured: without a"
                         " login, the UI serves loopback only")
    if bind is not None and bind not in LOOPBACK_HOSTS:
        raise ValueError(f"refusing to serve on {bind}: without"
                         " [web.auth], the UI serves loopback only")


# -- signed cookies ---------------------------------------------------------

def _b64(data: bytes) -> str:
    return base64.urlsafe_b64encode(data).rstrip(b"=").decode()


def _unb64(text: str) -> bytes:
    return base64.urlsafe_b64decode(text + "=" * (-len(text) % 4))


class Signer:
    """JSON values in a cookie, signed with HMAC-SHA256 and expiring.
    Each purpose signs separately, so one cookie cannot stand in for
    another."""

    def __init__(self, secret: bytes):
        self.secret = secret

    def _sig(self, purpose: str, payload: str) -> str:
        return _b64(hmac.new(self.secret, f"{purpose}.{payload}".encode(),
                             hashlib.sha256).digest())

    def dumps(self, purpose: str, value: dict, ttl_s: float) -> str:
        payload = _b64(json.dumps(
            {"v": value, "exp": int(time.time() + ttl_s)},
            separators=(",", ":")).encode())
        return f"{payload}.{self._sig(purpose, payload)}"

    def loads(self, purpose: str, token: str | None) -> dict | None:
        """The value, or None when missing, tampered with or expired."""
        if not token or token.count(".") != 1:
            return None
        payload, sig = token.split(".")
        if not hmac.compare_digest(sig, self._sig(purpose, payload)):
            return None
        try:
            data = json.loads(_unb64(payload))
        except ValueError:
            return None
        if not isinstance(data, dict) or data.get("exp", 0) < time.time():
            return None
        return data.get("v")


# -- Launchpad --------------------------------------------------------------

class LoginError(Exception):
    """The login did not complete (declined, expired, Launchpad down)."""


class Launchpad:
    """The OAuth 1.0a calls of a Launchpad login. PLAINTEXT signatures
    over HTTPS, and no consumer secret: Launchpad needs no registration,
    the consumer key is just a name shown to the person."""

    def __init__(self, consumer_key: str, root: str = LAUNCHPAD,
                 api: str = LP_API, timeout: float = 30):
        self.key, self.root, self.api = consumer_key, root, api
        self.timeout = timeout

    def _call(self, req: Request) -> bytes:
        req.add_header("User-Agent", "ftbfs")
        try:
            with urlopen(req, timeout=self.timeout) as r:
                return r.read()
        except (urllib.error.URLError, TimeoutError) as e:
            raise LoginError(f"Launchpad: {e}") from None

    def _post(self, path: str, token: str = "", secret: str = "") -> dict:
        data = {"oauth_consumer_key": self.key,
                "oauth_signature_method": "PLAINTEXT",
                "oauth_signature": f"&{secret}"}
        if token:
            data["oauth_token"] = token
        body = self._call(Request(f"{self.root}/{path}",
                                  urlencode(data).encode(), method="POST"))
        out = {k: v[0] for k, v in parse_qs(body.decode()).items()}
        if not out.get("oauth_token") or not out.get("oauth_token_secret"):
            raise LoginError(f"Launchpad: unexpected answer from {path}")
        return out

    def request_token(self) -> tuple[str, str]:
        t = self._post("+request-token")
        return t["oauth_token"], t["oauth_token_secret"]

    def authorize_url(self, token: str, callback: str) -> str:
        return f"{self.root}/+authorize-token?" + urlencode({
            "oauth_token": token, "oauth_callback": callback,
            "allow_permission": "READ_PUBLIC"})

    def access_token(self, token: str, secret: str) -> tuple[str, str]:
        """Fails (LoginError) when the person chose "No Access"."""
        t = self._post("+access-token", token, secret)
        return t["oauth_token"], t["oauth_token_secret"]

    def whoami(self, token: str, secret: str) -> str:
        auth = ", ".join(f'{k}="{v}"' for k, v in {
            "oauth_consumer_key": self.key, "oauth_token": token,
            "oauth_signature_method": "PLAINTEXT",
            "oauth_signature": f"&{secret}",
            "oauth_timestamp": str(int(time.time())),
            "oauth_nonce": uuid.uuid4().hex,
            "oauth_version": "1.0"}.items())
        me = json.loads(self._call(Request(
            f"{self.api}/people/+me", headers={
                "Authorization": f'OAuth realm="https://api.launchpad.net/",'
                                 f" {auth}",
                "Accept": "application/json"})))
        name = me.get("name", "")
        if not LP_NAME.match(name):
            raise LoginError("Launchpad: no person name in +me")
        return name

    def teams(self, name: str) -> set[str]:
        """The public teams `name` belongs to, directly or not. Read
        anonymously: private memberships do not count."""
        out: set[str] = set()
        url = f"{self.api}/~{name}/super_teams?ws.size=300"
        while url:
            page = json.loads(self._call(Request(
                url, headers={"Accept": "application/json"})))
            out |= {e["name"] for e in page.get("entries", [])}
            url = page.get("next_collection_link")
        return out
