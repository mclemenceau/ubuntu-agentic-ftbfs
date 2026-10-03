# Security

## Reporting a vulnerability

Please report it privately through GitHub: on the repository page, open
**Security**, then **Report a vulnerability**. Do not open a public
issue.

## Threat model

ftbfs reads public build failures, asks LLM agents for diagnoses and
fixes, and builds the results. Most of what flows through it was
written by someone else, so the question for every piece of data is
whether it is trusted.

**Trusted:**
- the operators: whoever has a shell on the host
- people given a role in `[web.roles]`, for what that role allows
- the configuration: `config.toml`, `config.local.toml`,
  `pipeline.toml`, `rules.toml`, `prompts/`, plugins
- the agent CLIs (`claude`, `opencode`), sbuild, LXD and the Ubuntu
  archive tooling

**Untrusted:**
- web visitors without a role (anyone, when the UI is public)
- the FTBFS page from qa.ubuntuwire.com, served over plain HTTP
- build logs and package sources: anyone can upload to a PPA, and a
  package's own files can carry prompt injection aimed at the agents
- everything an LLM writes: verdicts, notes, patches, debdiffs
- the Ubuntu `Sources` index (plain HTTP, not checked against its
  signature), Debian's indexes, UDD and reproducible-builds data

## Boundaries

**Ingest.** The FTBFS page is parsed strictly: source names, versions,
arches and the series must have Debian's or Launchpad's shape, build and
log links must be under `https://launchpad.net/`. One bad field rejects
the whole page. These values then become paths and command arguments;
commands are always argument lists, never shell strings.

**Agents.** Triage and diagnose get no tools at all. The dev agent gets
read and edit tools only, no shell and no network tools, in a fresh
unpacked tree:
- its git repository is kept outside the tree, git ignores the system
  and global config and runs no hooks or fsmonitor, and dev refuses to
  continue if the repository changed during the agent run
- symlinks leading out of the tree are removed while the agent runs;
  a package with such a link in `debian/` is refused
- code, not the agent, does the Debian mechanics (quilt, dch,
  dpkg-source); none of it runs package code
- each backend gets an explicit tool allowlist and a private config
  (no MCP servers, plugins, skills or the operator's settings)

The agent's output is only ever data: shown in the UI (escaped, under a
Content-Security-Policy that allows no foreign script), written to
reports, and built.

**Builds.** Package code (`debian/rules`, test suites) runs only inside
a build:
- with LXD builders, in a worker container, restarted after a killed
  build
- with the local builder (no `[builders.*]`), in sbuild's unshare
  chroot on the host: a user namespace with no network, as the
  operator's user. Prefer LXD builders on a machine that holds
  anything of value.

**Outward actions.** Stages that touch Launchpad, Debian or a forge
always wait at a manual gate. Nothing is uploaded or filed
automatically: a verified debdiff is a proposal for a human to review.

**Web UI.** It answers only to the host names in `[web]
allowed_hosts`. Without `[web.auth]` it refuses any name or bind beyond
loopback, and whoever reaches it is an operator. With it:
- reading is public, except costs (pages, event payloads, reports,
  console totals and the raw `usage.json`, `transcript.jsonl` and
  `investigation.md` files), which need the `viewer` role
- every POST route requires a role (`reviewer` or `operator`), checked
  on the server by the dependency each route declares; a test fails on
  a POST route without one
- people log in with Launchpad (read-public access only); the session
  is a cookie signed with HMAC-SHA256 by `[web] session_secret_file`
  (mode 600), `HttpOnly`, `SameSite=Lax`, `Secure` over HTTPS, expiring
  after `session_days`. Rotating the secret logs everyone out.
- POSTs must come from htmx (a header a cross-site form cannot send)
  with an `Origin` naming the host they are sent to
- runs started from the web stop being accepted past `[web]
  daily_cost_cap` per UTC day, and at most `[web] max_streams` live
  streams are open at once
Files are served from `work/`, `cache/` and the snapshots only, after
resolving symlinks.

**Secrets.** The OpenRouter key lives in a file outside the repository
(mode 600). opencode gets it as a file reference, so it never appears
in a command line, a config dump or an artifact.

## Known limits

- **Agent path confinement depends on the backend.** opencode denies
  paths outside the working directory. The `claude` backend allows its
  read and edit tools without a path scope, so a prompt-injected dev
  agent could read any file the operator's user can read and put it in
  its answer. Until agents run in an OS sandbox, run ftbfs as a
  dedicated user that holds nothing but its own key.
- **Plain HTTP sources.** A network attacker can make the FTBFS page
  or the Ubuntu `Sources` facts wrong (not unsafe: see Ingest), which
  can mislead triage.
- **Roles last a server restart.** A role removed from the config ends
  when `ftbfs serve` restarts with it; team membership is checked at
  login, so leaving a team ends its role at the next login or when the
  session expires.
- **The UI speaks plain HTTP.** Put it behind a TLS proxy before it is
  reachable from another machine: the session cookie is a bearer
  token.
