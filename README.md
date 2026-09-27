# ftbfs - agentic review of Ubuntu FTBFS

Reviews the failures listed on <http://qa.ubuntuwire.com/ftbfs/> through a
pipeline of stages. Most stages are deterministic; LLM agents only run
where judgement is needed, on small precomputed inputs.

## Quick start

```sh
uv sync
uv run ftbfs ingest                  # fetch + parse + diff the page
uv run ftbfs list --by arch          # what the default filter selects
uv run ftbfs run                     # run the pipeline on the selection
uv run ftbfs clusters                # failure clusters, rule hit rate
uv run ftbfs signals -v              # Debian/upstream facts per package
uv run ftbfs verdicts                # triage + diagnosis per cluster
uv run ftbfs status                  # runs, per-stage counts, gates, cost
```

The selection defaults come from `config.toml` (`[filter]`). The CLI flags
override them: `--component`, `--state F`, `--arch`, `--pocket`,
`--packageset`, `--team`, `--source 'python-*'`, `--include-bugged`,
`--limit`, `--profile NAME`.

Day-to-day operation (cadence, review routine, what to watch) is in
`docs/OPERATIONS.md`.

`ftbfs export --out ftbfs.json` writes the selection as JSON, including
build and log URLs. Every ingest also keeps a full snapshot in
`state/snapshots/`.

## Seeing what happens

| Command | Shows |
|---|---|
| `ftbfs why <source or item>` | filter exclusions, then per-stage decision (waiting, `when` false, gated, cached, ...) |
| `ftbfs show <source>` | event timeline and stage results |
| `ftbfs tail -f [--unit X] [--stage Y]` | live event stream |
| `ftbfs status` | runs and latest result per unit/stage |

Artifacts live under `work/<source>/<version>/<arch>/<stage>/`. For agent
stages that includes `attempt-N/{prompt.md,transcript.jsonl,result.json,
usage.json}`.

Control commands:
- `ftbfs approve <stage> <unit...> [--reject]`
- `ftbfs retry <stage> <unit...>`
- `ftbfs control pause|resume|cancel`

## Web UI and reports

```sh
uv run ftbfs serve                   # http://127.0.0.1:8047
uv run ftbfs report --source xfaces  # work/<src>/<ver>/investigation.md
```

The UI is a separate process over the same SQLite database (WAL), so it
can be restarted without disturbing a run. Pages:
- **Overview**: latest snapshot, delta since the previous one, the
  pipeline DAG with per-stage counts (click through to the items), runs
- **Run**: live per-stage counters, agents and builds in flight, running
  cost, event stream (Server-Sent Events), pause/resume/cancel
- **Console**: an agent's transcript as it streams (tool calls, edits,
  results), and a kill button for a runaway agent
- **Package**: the rendered investigation report, pipeline dots per arch,
  `why` per stage with approve/retry buttons, every attempt's artifacts
  (prompt, transcript, debdiff, build log) and the event timeline
- **Gates**: every stage waiting for approval, with triage/diagnosis
  context; **Attention**: errors, needs-human, exhausted loops
- **Signals**: packages per facts signal with their Debian versions and
  bugs; signal badges also show (and filter) on Items, Clusters and
  Package pages
- **Items**, **Clusters**, **Costs** (by stage/model, run and package),
  **Snapshots** (new, regressed, gone, state changes between any two)

Controls go through the same code as the CLI and are recorded as events.
The server binds to loopback, refuses foreign Host headers and only
accepts POSTs sent by htmx; it has no authentication, so do not expose it.

`investigation.md` is stitched from per-stage Jinja partials in DAG
order (`ftbfs/templates/stages/<stage>.md.j2`), with no tokens. A plugin
stage can ship `plugins/templates/stages/<name>.md.j2` (which also
overrides a built-in one); a stage without a partial gets a generic
section. The summary and recommended next action are derived from the
results.

## Pipeline

`pipeline.toml` is the DAG. Each `[stage.<name>]` block can set:
- `after`
- `when`: a restricted expression over upstream results, e.g.
  `"triage.fixable != 'no' and item.arch == 'amd64'"`
- `gate = "manual"`
- `agent = {backend, tier, ...}`
- `on_fail = {goto, max_loops}`
- `enabled = false`

A stage re-runs only when its inputs change: its version or options, the
agent spec, the unit, upstream results, or a retry/loop-back. Everything
else is cached.

### Adding a stage

Drop a module in `ftbfs/stages/` (or a file in `plugins/`, or register an
`ftbfs.stages` entry point):

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

Then enable it:

```toml
[stage.my_review]
after = ["verify"]
agent = { backend = "opencode", tier = "large" }
on_fail = { goto = "dev", max_loops = 2 }
```

`outward` stages (anything that touches Launchpad, Debian or a forge)
always get a manual gate.

### Agent backends

Stages ask for a tier (`small|medium|large`). `config.toml` maps tiers to
models per backend (`[backend.<name>.tiers]`). Backends implement
`ftbfs.agents.base.AgentBackend`.

The `claude` backend runs `claude -p`, isolated from your interactive
setup: no CLAUDE.md, settings, MCP servers, skills or session files. The
stage supplies its own system prompt, so a tool-less call costs about 400
input tokens of overhead instead of about 23k. Output is enforced with
`--json-schema` and re-validated. Per stage, `agent = {...}` in
`pipeline.toml` can set:
- `tier`
- `effort` (low to max): thinking tokens dominate output cost
- `max_budget_usd`
- `timeout`
- `max_turns` (unset: the backend's own limit)

The `opencode` backend runs `opencode run --format json` with models as
`provider/model` ids (e.g. `openrouter/anthropic/claude-sonnet-5`).
`[backend.opencode] api_keys` points each provider at a dedicated key
file, passed to opencode as a `{file:...}` reference so the key never
lands in artifacts; a missing file is an error, never a fallback.
Providers without an entry use opencode's own login. It is isolated the
same way: a private config home under `state/opencode/`, no
global or project config, MCP servers, plugins, skills or `~/.claude`
rules. Each call gets one inline agent whose permissions are an explicit
allowlist built from the stage's tool policy, and nothing outside the
working directory is reachable. Differences from `claude`:
- no structured output: the schema goes in the prompt, and the answer is
  validated and repaired once with the small tier
- `effort` maps to the model's `--variant`, which is not calibrated like
  claude's `--effort` (e.g. Sonnet at `medium` often does not think)
- `max_budget_usd` is enforced by summing step costs and killing the
  agent
- sessions are kept in opencode's own database, titled
  `ftbfs <unit>/<stage>/attempt-N`

Switch every agent stage with `default_backend = "opencode"` in
`config.toml`, or one stage with `agent = { backend = "opencode", ... }`.
The agent spec is part of the cache key, so switching re-runs that stage
(and whatever depends on its result).

## LLM stages

- `triage` (cluster, small tier, ~12 clusters packed per call): category,
  summary, obvious, fixable and action. Clusters the facts already decide
  get no LLM call: blocked dependencies, or every package fixed or newer
  and building in Debian.
- `diagnose` (cluster, medium tier, medium effort): root cause, evidence,
  fix kind and strategy, patch outline, risk. It runs only for
  patch/investigate-type verdicts.

Prompts live in `prompts/*.md`; editing one re-runs only that stage and
what follows it. `--sample-clusters N --seed S` selects random whole
clusters, for trying prompt or model changes cheaply.

## Failure rules

`rules.toml` classifies excerpts into failure classes and clusters with no
tokens spent. Rules are tried in order. Editing the file re-classifies on
the next run.

## Debian and upstream facts

The `facts` stage runs once per source package, with no tokens. Sources:
- Ubuntu and Debian Sources indexes, reproducible-builds testing status:
  cached once a day in `cache/facts/`
- one query per batch to the public UDD mirror, for Debian FTBFS bugs

It emits signals that answer "is this known or fixed in Debian?" before
any LLM runs:
- `sync-candidate`, `merge-candidate`, `newer-in-experimental`,
  `not-in-debian`
- `fixed-in-debian`, `debian-ftbfs-open`, `debian-patch`
- `ftbfs-in-debian-testing`, `builds-in-debian-testing`

`ftbfs signals --signal fixed-in-debian` lists the packages for a signal, as
does the **Signals** page of the web UI.

## Development

```sh
uv run pytest
uv run ruff check .
```

`make help` lists shortcuts for the common tasks: `make check` (lint +
tests), `make serve`, `make run ARGS='--source xfaces'`, etc.
