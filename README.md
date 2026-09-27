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
uv run ftbfs status                  # runs, per-stage counts, gates, cost
```

The selection defaults come from `config.toml` (`[filter]`). The CLI flags
override them: `--component`, `--state F`, `--arch`, `--pocket`,
`--packageset`, `--team`, `--source 'python-*'`, `--include-bugged`,
`--limit`, `--profile NAME`.

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

`ftbfs signals --signal fixed-in-debian` lists the packages for a signal.

## Development

```sh
uv run pytest
uv run ruff check .
```
