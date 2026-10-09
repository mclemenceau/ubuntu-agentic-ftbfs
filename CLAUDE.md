# Notes for coding agents

ftbfs triages Ubuntu "fails to build from source" packages: a DAG of
stages (deterministic first, LLM agents only for judgement), state in
SQLite, a server-rendered web UI (FastAPI, Jinja, htmx) with Launchpad
login. Python 3.12, uv, ruff, pytest.

## Where things are

- `docs/DESIGN.md`: architecture and why. Read the relevant section
  before changing a mechanism.
- `CONTRIBUTING.md`: conventions, adding a stage or a rule, cheap runs.
  Its rules apply to every change.
- `docs/OPERATIONS.md`: CLI and UI usage; `docs/DEPLOYMENT.md`: the
  server, updates and rollback; `SECURITY.md`: trust boundaries.
- `docs/STATUS.md`: history and roadmap; open issues are on GitHub
  (`gh issue list`).
- Code: `ftbfs/cli.py` (commands), `ftbfs/core/` (scheduler, stage
  API), `ftbfs/stages/` (one file per stage), `ftbfs/agents/`
  (claude, opencode, fake backends), `ftbfs/builder/` (local sbuild,
  LXD workers), `ftbfs/web/` (`app.py` routes, `auth.py` roles),
  `ftbfs/templates/`, `prompts/`, `pipeline.toml` (the DAG),
  `rules.toml` (log classifier), `config.toml` (defaults, merged under
  the git-ignored `config.local.toml`).

## Checks

`make check` (ruff and pytest) must pass before every commit. Tests
need no network, API key, sbuild or LXD. CI runs the same steps.

## Rules that are easy to miss

- **Paid re-runs.** A change to a stage `version`, its `inputs()`, its
  options or agent spec in `pipeline.toml`, a prompt, or `rules.toml`
  invalidates cached results and re-runs paid agent stages on every
  existing database. Avoid it unless the result should change, and say
  so in the commit message (CONTRIBUTING, "Changes that re-run paid
  stages").
- **Web routes.** Every POST route declares its role (`Reviewer` or
  `Operator`) and passes `user.by` to `App`; anything showing costs
  checks `user.costs`. `tests/test_auth.py` enforces both.
- **Outward actions** (Launchpad, Debian, forges) always sit behind a
  manual gate.
- Every source file starts with the copyright and SPDX notice
  (`tests/test_license.py`). 80 columns, no em dashes.
- Bug fixes start with an end-to-end reproduction, then a test that
  fails without the fix.

## Running it locally

Never give a development checkout the `[builders.*]` names of a
running instance: both would drive the same LXD workers. Use no
builders (plain sbuild), the `fake` agent backend, `--until classify`
or `--stage facts` for free runs, and `ftbfs serve --port 8048` next
to another instance.

## Releases

Bump `version` in `pyproject.toml`, `uv lock`, commit
`Release X.Y.Z: <headline>`, signed tag `vX.Y.Z`, GitHub release with
a short note. Deploying it is `docs/DEPLOYMENT.md`, step 9.
