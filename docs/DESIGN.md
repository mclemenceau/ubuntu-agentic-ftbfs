# Design

How ftbfs is built and why. Each section is one decision: the choice,
the reasons, and what was rejected. For running the system see
`OPERATIONS.md`; for changing it, `../CONTRIBUTING.md`; for the
security boundaries, `../SECURITY.md`.

## The problem

The Ubuntu development series carries around a thousand failing builds
at any time (<http://qa.ubuntuwire.com/ftbfs/>). Most share a handful of
causes (a new compiler, glibc or CMake, a missing dependency), many are
already fixed in Debian, and each needs someone to read a log of about
140 KB before anything else can happen. ftbfs does that reading, sorts
the failures, and for the ones worth fixing in Ubuntu proposes a
debdiff that has been built, for a human to review and upload.

## Overview

```
ingest
  |
excerpt -> classify --+
                      +--> triage --> diagnose ----------+
facts ----------------+      |                           v
                             +--> reproduce --> [gate] dev
                                                          ^   |
                                                     fail |   v
                                                          +- verify
```

| Stage | Unit | Kind | Does |
|---|---|---|---|
| excerpt | item | code | cut the log down to the failure, ~3 KB |
| classify | item | code | `rules.toml`: failure class and cluster |
| facts | package | code | Debian versions, bugs, reproducible builds |
| triage | cluster | LLM, small | category, fixable, action |
| diagnose | cluster | LLM, medium | root cause, fix strategy, risk |
| reproduce | item | build | sbuild the failing version, compare |
| dev | item | LLM agent | edit the source; code makes the debdiff |
| verify | item | build | sbuild the fix; on failure back to dev |

An *item* is one failing build (source/version/arch), a *package* one
source, a *cluster* the items that fail for the same normalized reason.
The code is in `ftbfs/`: `core/` (pipeline, scheduler, context),
`stages/`, `agents/`, `builder/`, `facts/`, `web/`, and `app.py`, the
operations shared by the CLI and the web UI.

## Deterministic first, LLMs only for judgement

**Choice.** Every step that code can do is code: fetching, cutting logs,
classifying, comparing versions with Debian, building, writing patches
and changelogs. LLMs are used for three judgements only: what kind of
failure this is (triage), why it happens and how to fix it (diagnose),
and the source edit itself (dev). Each runs on the cheapest tier that
does it well: triage on the small tier, diagnose on the medium tier,
dev on the medium tier with the large tier for its last retry.

**Why.** Cost scales with the number of failures, and code is also
reproducible and testable. A judgement an LLM got wrong is visible
because everything around it is deterministic.

**Rejected.** One agent per package that reads the log, investigates
and fixes it in one session: simpler to write, but every package would
pay for the full log and a long conversation, results would not be
comparable across packages, and nothing would be cached.

## Excerpts: no stage ever sees a raw log

**Choice.** The excerpt stage reads sbuild's summary, finds the
terminal error (make `***`, `dh_*`, ninja), walks back to the first
real error and keeps at most 150 lines around it. It also computes a
normalized signature of the error lines, stable across paths, versions
and arches. Everything downstream gets the excerpt, never the log.

**Why.** This is the main token saver: the median excerpt is about
3 KB, 308 times smaller than the logs it comes from (measured on the
first full ingest, 2026-09-27). The signature is what lets reproduce
tell "same failure" from "another failure" with no LLM.

**Rejected.** Sending the log tail, or letting the model grep the log
with tools: the tail often misses the first error, and tool calls cost
more turns than the excerpt costs tokens.

## Clusters as the unit of LLM work

**Choice.** `rules.toml` maps an excerpt to a failure class and a
cluster id (by class, by a captured key such as the missing tool, by
signature or by package). Items no rule matches cluster by signature.
Triage and diagnose run once per cluster, on a context pack: the
representative item's excerpt (amd64 first), a few key lines from other
members, and the package facts. Cluster membership is global: every
active classified item, not only the run's selection.

**Why.** Failures repeat: on 2026-09-30, 872 items formed 375 clusters,
and the 32 items of the C23 `const` cluster got one diagnosis. Global
membership keeps a cluster's inputs, and so its cached result, the same
whatever filter a run uses: a run on `--source hexcurse` and a full run
agree on hexcurse's cluster.

**Rejected.** Clusters per run (cached verdicts would change with the
filter), and asking an LLM to cluster (cost on every item, and not
stable from one run to the next). Rules cover 66% of items; the rest
cost one triage each until a rule is written, which is the cheapest
improvement there is.

## Facts before tokens

**Choice.** The facts stage compares Ubuntu and Debian versions (the
`Sources` indexes), asks UDD for Debian FTBFS bugs (one query per 200
packages) and reads the reproducible-builds status in Debian testing.
It emits signals (`sync-candidate`, `fixed-in-debian`,
`debian-ftbfs-open`, ...). Triage decides some clusters from them with
no LLM call: blocked dependencies wait, and clusters whose packages are
all fixed in Debian, or newer there and building in testing, are a sync
or a merge.

**Why.** "Is this already fixed in Debian?" is the first question a
Ubuntu developer asks, and it has an exact answer. On 2026-09-30, 65 of
425 triage verdicts (39 sync, 6 merge, 20 wait-dependency) cost nothing.

**Rejected.** Letting triage find this out from the excerpt: it cannot
see Debian, and would guess.

## A DAG of plugin stages, cached by inputs

**Choice.** `pipeline.toml` declares the stages and their `after`
dependencies; each stage is a class registered from `ftbfs/stages/`,
a `plugins/*.py` file or an `ftbfs.stages` entry point. The scheduler
runs them in DAG order and stores one result per unit and stage. A
result is reused while its inputs hash is unchanged: the stage version,
its options, its agent spec, the unit, the upstream results it depends
on, the stage's own inputs (its prompt, `rules.toml`, the day for
facts) and a nonce bumped by a retry. `when` predicates over upstream
results decide whether a stage applies, `gate = "manual"` waits for a
person, and `on_fail` loops back to an earlier stage a bounded number
of times.

**Why.** New steps (an adversarial review, filing a bug) must be one
class plus a config block, without touching the scheduler. Caching by
inputs makes running again always safe and cheap: editing a prompt
re-runs that stage and what follows it, nothing else.

**`when` is a restricted evaluator** (`ftbfs/core/expr.py`): literals,
names, attribute and constant-subscript access, comparisons, `and`,
`or`, `not`. Anything else is rejected at parse time, and a missing
name is `None`, not an error. Python's `eval` was rejected: the
config would then be able to run code, and a typo would crash a run
instead of skipping a stage.

**The cost of this choice:** anything in the hash is a migration. A
renamed option, a version bump or a prompt edit re-runs that stage and
everything after it, including paid agent stages, so they are made on
purpose and announced in the commit.

## `ftbfs run` is a pass, not a daemon

**Choice.** Each `ftbfs run` makes one pass over the DAG for the
selection and exits. Whatever cannot move yet (a gate, a pending build,
a stage waiting upstream) is picked up by the next run. State is in
SQLite (WAL mode): runs, results, gates, events, dispositions. The web
UI is a separate process over the same database. A cron job or a
systemd timer does the scheduling.

**Why.** No long-lived state in memory means nothing to lose on a crash
or a reboot: a killed run leaves results for the units it finished, and
the next run carries on. The UI can be restarted at any time without
disturbing a run, and a run started from the UI is a detached
`python -m ftbfs run --trigger web`, the same code path as the CLI.

**Rejected.** A daemon with a job queue: more moving parts, the same
outcome. A server database (PostgreSQL): one writer at a time is enough
here, and SQLite needs no service.

## The agent edits files; code does the Debian mechanics

**Choice.** The dev agent gets the unpacked source, the excerpt (from
the local reproduction when there is one), the diagnosis and the facts,
and only read and edit tools. When it is done, code turns its edits
into a DEP-3 quilt patch, adds the changelog entry with the next Ubuntu
version, runs `update-maintainer`, builds the source package and the
debdiff (`ftbfs/builder/srcpkg.py`). Each attempt saves its raw edits
as `edits.diff`; a retry after a failed verify replays the edits of
the latest successful attempt on a fresh tree, and the agent adds to
them, with the new build error.

**Why.** Patch headers, version numbers and changelog format have one
right answer each, so code gets them right every time and the agent's
turns go to the actual fix. No shell also means nothing the agent writes
runs on the host (see `SECURITY.md`). Cumulative retries mean the second
attempt fixes the next error instead of starting over: libevhtp's
retry fixed its second CMake 4 error on top of the first.

**Rejected.** An agent with a shell that builds and iterates on its
own: faster loops, but it runs package code on the host, and its
mechanics vary from run to run. Retries from scratch: they lose the
previous attempt's work.

## Isolated agents

**Choice.** Both real backends run the CLI with nothing of the
operator's setup. `claude -p` gets no CLAUDE.md, settings, MCP servers,
skills or session files, and the stage's own system prompt. opencode
gets a private config home under `state/opencode/`, no global or project
config, and one inline agent whose permissions are an explicit
allowlist built from the stage's tool policy. Output is checked against
a JSON schema (natively with claude; in the prompt with opencode, with
one repair on the same tier when the answer does not validate).

**Why.**
- *Overhead:* a tool-less `claude -p` call costs about 400 input tokens
  this way, against about 23k with the default setup.
- *Determinism:* the same prompt gives comparable answers on any
  machine, whatever the operator has installed.
- *No leakage:* the operator's instructions, memories and tools do not
  reach a prompt built from untrusted package sources.

## Tiers, not models; two backends

**Choice.** Stages ask for a tier (`small`, `medium`, `large`) and an
effort; `config.toml` maps tiers to models per backend. `claude` uses
the Claude CLI (a subscription or key); `opencode` reaches the same
Claude models through OpenRouter, billed to a dedicated key file whose
credit limit is the hard cap on spending. `fake` returns canned answers
for tests. opencode is the default.

**Why.** Model choice is a cost decision that changes over time; stages
should not change with it. The A/B on a 30-cluster sample (2026-09-27)
showed opencode's triage matching claude's about as well as claude
matches itself, at 40% of the cost, and diagnose at opencode's `high`
variant matching claude's depth at about half its cost. A dedicated key
also separates the pipeline's bill from interactive use.

**Rejected.** A model name per stage in `pipeline.toml`: switching
providers would mean editing every stage.

## Builds: one image, one worker per slot

**Choice.** Reproduce and verify build with sbuild in unshare mode
against `<series>-proposed`, as Launchpad does. Builders are declared in
`config.local.toml`. An LXD builder runs one worker container per slot,
all from one image (`ftbfs builders image`: sbuild, mmdebstrap, the
chroot tarball); a build goes to the builder with the most free slots
for its arch. A worker is force-restarted the first time a run uses it
and after a killed build, and a lock per worker keeps two runs apart. A
builder that fails before sbuild starts is marked down for ten minutes
and the build moves on; if every builder for the arch is down, the unit
is pending, not failed. Without builders, plain sbuild runs on the
host.

**Why.** One image means a signature mismatch is never a host
difference. Workers per slot keep builds from sharing state, and package
code runs in a container, not on the host. Marking a host down instead
of failing the unit keeps an unreachable machine from turning into
dozens of errors to retry. Local builds are free, so reproduce has no
gate.

**Rejected.** Per-host chroots maintained by hand (drift between
hosts), and Launchpad PPAs for every build (slow, outward, and they need
an upload key). The PPA path exists for foreign arches but is not set
up (see `STATUS.md`).

## Outward actions are always gated

**Choice.** A stage of kind `outward` (anything that touches Launchpad,
Debian or a forge) always gets a manual gate, whatever `pipeline.toml`
says. Today no stage files or uploads anything: the end product is a
verified debdiff in `work/` and on the Next steps page. dev is gated
too, since it is where most of the money goes.

**Why.** "Verified" means it builds, not that the change is right or
minimal. A person reviews every debdiff and decides what reaches the
archive or a bug tracker.

## A server-rendered UI with htmx

**Choice.** FastAPI and Jinja templates, htmx for partial updates and
Server-Sent Events for live runs and agent transcripts. htmx is
vendored (`ftbfs/web/static/`); there is no JavaScript build. All SQL
lives in `ftbfs/web/queries.py`. Every control (approve, retry,
dispose, start, pause, kill) goes through `App`, the same code as the
CLI, and records an event.

**Why.** The pages are tables and logs; a server-rendered page is the
simplest thing that does that, and one language is easier to maintain
than two. Controls shared with the CLI cannot drift from it.

**Rejected.** A single-page app with a JSON API: a build toolchain and
a second codebase for no capability the pages need.

## Web access: public reading, Launchpad login for actions

**Choice.** Anyone can read every page except costs. Acting needs a
Launchpad login and a role from `[web.roles]` in the local config:
`viewer` sees costs, `reviewer` decides gates, dispositions and retries,
`operator` starts, pauses and cancels runs and kills agents (each role
includes the ones before it). Roles name Launchpad people or teams.
The login is Launchpad's OAuth 1.0a flow, asking only for read access
to public data; `+me` names the person, the token is then dropped, and
a signed cookie (`HttpOnly`, `SameSite=Lax`, 30 days) holds the name and
the role-granting teams seen at login. The role is worked out from the
config at each request. Each POST route declares the role it needs
(`require`), and a test walks every route so a new one cannot forget.
Events and gates record `web:<login>`. Without `[web.auth]`, the UI
serves loopback only and its user is an operator: no login on a laptop.
On top of the roles: the Host check (`[web] allowed_hosts`, which also
stops DNS rebinding), POSTs only from htmx with an `Origin` of this very
host, a strict Content-Security-Policy, a daily cost cap on runs
started from the web, and a bound on open live streams.

**Why.** The data comes from public build logs, and a public view helps
the Ubuntu developers who would act on it; money is the one thing kept
for people with a role. Launchpad is the identity Ubuntu developers
already have, and its teams (`~ubuntu-dev`, a review team) map to roles
without a separate allowlist to maintain. The flow needs no registered
application or client secret. Roles in the config, not the database,
make "who can spend money" a config review.

**Rejected.**
- GitHub OAuth: an OAuth app and a client secret to manage, and an
  identity that is not the one Ubuntu work uses.
- oauth2-proxy in front: one more service, and the app would trust a
  header that anything reaching it directly could forge.
- Ubuntu One (OpenID 2.0): few maintained libraries.
- Local passwords: storage and resets to maintain.
- Roles granted from the database or the UI: a compromised reviewer
  session could promote itself.

**Costs of the choice.** Launchpad's consent page reads like an API
grant ("Read non private Data"), and each login leaves one read-only
token in the person's "Authorized applications" list, which only they
can revoke. Team membership is read at login and only when public.

## Evidence

Measured on the live instance; dates are when they were measured.

| What | Result | Date |
|---|---|---|
| Log to excerpt | 308x smaller, median ~3 KB | 2026-09-27 |
| Items matched by a rule | 66% (578 of 872), 375 clusters | 2026-09-30 |
| Triage | $0.0034 per cluster (Haiku via OpenRouter, ~12 per call) | 2026-09-30 |
| Diagnose | $0.027 per cluster (Sonnet via OpenRouter, high variant) | 2026-09-30 |
| First full run, triage + diagnose | 410 + 283 clusters, $8.98 (estimate was $15) | 2026-09-27 |
| Reproduce | 127 of 146 reproduced (87%), ~20 s to minutes each | 2026-09-30 |
| dev + verify | 38 of 44 approved units verified (86%) | 2026-09-30 |
| dev cost | $13.54 over 101 attempts: $0.36 per verified fix | 2026-09-30 |
| All LLM spend since the start | $31.20, including experiments and A/B | 2026-09-30 |

"Verified" is sbuild's verdict. Of the 38, one had been reviewed and
accepted by 2026-09-30; the rest wait for review on the Next steps page.
