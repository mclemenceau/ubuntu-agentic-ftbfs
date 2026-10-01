# Contributing

Thanks for looking. Read `docs/DESIGN.md` first: it explains how the
pipeline is built and why, and most review comments come back to it.
Bugs and ideas go to GitHub issues; security problems go through
`SECURITY.md` instead.

## Setup

```sh
sudo apt install dpkg-dev devscripts quilt ubuntu-dev-tools
uv sync
make check                 # ruff + pytest
```

Python 3.12 or later. The packaging tools are used by the tests that
build a real source package. The tests need no network, no API key, no
sbuild and no LXD: the `fake` agent backend and fake `sbuild` and
`lxc` stand in for them, and CI runs the same `make check` steps
(`.github/workflows/ci.yml`).

`make help` lists the other shortcuts (`make serve`, `make run
ARGS='--source xfaces'`, ...).

## Conventions

- `ruff check .` and every test pass on every commit. A failure or a
  flaky test you come across is worth fixing even when your change did
  not cause it.
- Stay within 80 columns where reasonable, in code and docs.
- No em dashes; a plain `-` does.
- Every source file starts with the two-line notice (see any `.py`
  file); `tests/test_license.py` checks it.
- Prefer the simple, direct design, and quality over speed of writing
  it. A new mechanism needs a concrete reason.
- Commit subjects name the area and say what changed for the user, as
  in the history (`Builds: kill sbuild once its log stops growing`).
- A bug fix starts by reproducing the bug the way a user would hit it,
  then comes with a test that fails without the fix.
- Anything that touches Launchpad, Debian or a forge stays behind a
  manual gate. Nothing is filed or uploaded automatically.

### Changes that re-run paid stages

A stage result is reused while its inputs hash is unchanged (DESIGN,
"A DAG of plugin stages"). Changing any of these re-runs that stage and
everything downstream on every existing database, including the agent
stages that cost money:
- a stage's `version`, or what its `inputs()` returns
- a stage's options or agent spec in `pipeline.toml`
- a prompt in `prompts/` (its front matter is stripped, so the notice
  there is free)
- `rules.toml` (re-classifies everything, which is free, then re-runs
  triage and diagnose for the clusters that changed)

Do it when the result should change, and say so in the commit message.
Comments in `pipeline.toml` and `config.toml` are not hashed.

## Running the pipeline cheaply

Up to `classify` and `facts`, nothing costs tokens:

```sh
cp config.local.toml.example config.local.toml   # adjust it
uv run ftbfs ingest
uv run ftbfs run --until classify --limit 20
uv run ftbfs run --stage facts --limit 20
uv run ftbfs clusters
```

For agent stages, try a change on a few whole clusters before a full
run, and compare the verdicts:

```sh
uv run ftbfs run --sample-clusters 5 --seed 1 --until diagnose
uv run ftbfs verdicts --sample-clusters 5 --seed 1
uv run ftbfs status          # what it cost
```

A real agent backend is needed for that (`docs/OPERATIONS.md`,
"Choosing the agent backend"). The `fake` backend is for tests: it
answers `{}` unless a test gives it a responder.

`ftbfs serve --port 8048` serves the UI next to another instance on the
default port. Two checkouts with the same builder names in their
`config.local.toml` share the same LXD workers without knowing it: give
a development checkout no builders, or other names.

## Adding a stage

A stage is a class registered with `@register`, found in
`ftbfs/stages/`, in a `plugins/*.py` file, or through an `ftbfs.stages`
entry point:

```python
from ftbfs.core.stage import Kind, Stage, StageResult, Status, register

@register
class MyReview(Stage):
    name = "my_review"
    kind = Kind.AGENT          # deterministic | agent | build | outward
    version = "1"

    def run(self, ctx, unit_ids):
        out = []
        for uid in unit_ids:
            diff = ctx.upstream(uid, "dev")
            res = ctx.run_agent(uid, f"Review:\n{diff['debdiff']}",
                                output_schema=SCHEMA)
            status = Status.OK if res.data["ok"] else Status.FAIL
            out.append(StageResult(uid, status, res.data))
        return out
```

Then add it to the DAG in `pipeline.toml`:

```toml
[stage.my_review]
after = ["verify"]
agent = { backend = "opencode", tier = "large" }
on_fail = { goto = "dev", max_loops = 2 }
```

The keys of a `[stage.<name>]` block:
- `after`: the stages whose results must be ok first
- `when`: a restricted expression over upstream results, e.g.
  `"triage.fixable != 'no' and item.arch == 'amd64'"`
- `gate = "manual"`: wait for `ftbfs approve` (forced for `outward`)
- `agent = {backend, tier, effort, max_budget_usd, timeout, max_turns,
  escalate_after_loops, escalate_tier}`
- `on_fail = {goto, max_loops}`: loop back to an upstream stage
- `enabled = false`: keep the block, skip the stage
- any other key is an option passed to the stage

Also:
- Ask for a tier, never a model: `config.toml` maps tiers to models per
  backend.
- Return something from `inputs()` for anything the result depends on
  that is not an upstream result or an option (a prompt digest, a
  file), so the cache sees it change.
- A Jinja partial in `ftbfs/templates/stages/<name>.md.j2` (or
  `plugins/templates/stages/`) gives the stage its section in
  `investigation.md`; without one it gets a generic section.
- Tests use `FakeBackend(responder=...)`; see
  `tests/test_triage_diagnose.py`.
- `ftbfs stages` lists the registered stages and the configured DAG.

Agent backends implement `ftbfs.agents.base.AgentBackend` and are
listed in `ftbfs/agents/__init__.py`.

## Adding a failure rule

`rules.toml` classifies excerpts with no tokens; its header documents
the fields. Rules are tried in order, first match wins, so specific
rules go before generic ones. Good candidates are unknown (`sig:`)
clusters that span several packages (`ftbfs clusters`) and clusters a
diagnosis reports as mixed. After an edit:

```sh
uv run ftbfs run --until classify
uv run ftbfs clusters        # hit rate, and the clusters it formed
```

Add a case to `tests/test_logs_rules.py` with the log line that
motivated the rule.
