# Parallel Persona Load-Test

Load-testing a venue front-desk agent with parallel agents: 8 customer personas run concurrently,
each with its own isolated data layer, with every run accumulating into a single telemetry database.

## Hotdata

- One task-scoped database per persona: create → load (pricing / availability / venue_facts) → query →
  write replies back → drop. Eight concurrent personas means eight isolated databases, all torn down on completion.
- One telemetry database scoped to the whole event (never dropped), tables `runs` and `persona_runs`.
  Currently holds the morning's concurrency benchmarks plus 16 rows across v1 and v2.
  Every number in our demo is a live SQL query against it — see `demo_queries.sql`.

## RocketRide

- Each persona is an independent pipeline instance, launched concurrently with asyncio.gather and
  merged back in Python. All pipelines run on RocketRide Cloud.
- API keys and database ids are injected server-side via ROCKETRIDE_-prefixed variables,
  so no `.pipe` file contains a secret.

## What the telemetry changed

**1. Our first parallel design barely parallelized.**
We started by fanning out N agent branches inside a single pipeline. Telemetry showed 63.4s for
N=3 versus 86.2s running them one at a time — almost no gain. We switched to launching N independent
pipeline instances concurrently: N=3 dropped to 33.4s. At N=8, wall clock is 81.0s while the eight
turns sum to 440.3s — the wall clock equals the slowest single turn, a 5.4× speedup.

**2. A passing score that wasn't.**
Between v1 and v2 we changed two lines of the prompt: answer in the first sentence, and ask for date
and headcount before quoting. Pass rate went 6/8 → 7/8, average reply length 112.8 → 84.5 words (-25%),
wall clock unchanged. But if we had trusted only the deterministic rules, v2 would read 8/8.
The LLM judge disagreed on p7: the agent did ask for date and headcount, but only after dumping every
price first. Rules can check length and keyword presence; they cannot check ordering. The prompt fix
cured the symptom our rules could measure, not the behaviour underneath.

## Running it

### Setup

```bash
.venv/bin/python -m pip install -r requirements.txt
```

The [Hotdata CLI](https://hotdata.dev) (`hotdata`, 0.33+) must be on `PATH` — the database lifecycle
and every telemetry write shell out to it.

### `.env`

Six variables, no values here. `.env` is gitignored and no `.pipe` file contains a secret.

| Variable | Used for |
| --- | --- |
| `ROCKETRIDE_API_KEY` | RocketRide Cloud auth, passed to the client explicitly |
| `ROCKETRIDE_URI` | API host — `https://api.rocketride.ai`, not the Studio UI host |
| `ANTHROPIC_API_KEY` | Re-exported server-side as `ROCKETRIDE_ANTHROPIC_KEY` for the pipeline's `llm_anthropic` node, and used directly by the LLM judge |
| `HOTDATA_API_KEY` | The standard API token (not the Database API token — that one cannot delete databases) |
| `HOTDATA_WORKSPACE_ID` | Hotdata workspace |
| `HOTDATA_TELEMETRY_DB_ID` | The telemetry database to reuse. Set, it is reused; unset, one is created and the line to add is printed; set but pointing at nothing, the run refuses rather than silently starting an empty one |

Scripts load these with `load_dotenv()`, which puts them in the process environment, so the `hotdata`
subprocesses inherit them — no manual `source` needed.

### The persona run

```bash
# Generate one single-branch pipe per persona for a prompt version
.venv/bin/python gen_pipe.py --n 8 --serial --prompt-version v1

# Run all 8 personas concurrently, grade them, write telemetry
.venv/bin/python run_v1.py --n 8 --concurrency 8 --prompt-version v1
```

Writes `transcripts_<version>.md` (per persona: first message, the rows retrieved from its database,
the reply, rule results, judge verdict, database lifecycle timings) and one `persona_runs` row per turn.
Useful flags: `--rate-limit-floor 4` (concurrency the gate drops to on the first rate limit),
`--judge-model`, `--no-telemetry`.

### The architecture benchmark

`run_loadtest.py` is the older, narrower harness behind finding 1 above — it times the two pipeline
*shapes* against each other and does no grading, no per-persona database, and no telemetry:

```bash
.venv/bin/python gen_pipe.py --n 3               # one N-branch fan-out pipe
.venv/bin/python gen_pipe.py --n 3 --serial      # N single-branch pipes
.venv/bin/python run_loadtest.py --n 3 --mode both   # prints the speedup
```

`bench.py` is the same comparison plus the concurrent mode, recording every result to
`runs`: `.venv/bin/python bench.py --modes serial A B --ns 2 3`.

### Smoke tests

```bash
.venv/bin/python smoke_rr.py        # one turn through the minimal pipeline
.venv/bin/python smoke_hotdata.py   # agent-side HTTP tool probe (known to fail on Cloud)
```

`NOTES.md` holds the verified environment facts — read it before changing anything.
