# NOTES

Verified facts about this project's environment and tooling.
**Read this file before starting any task.**

Everything here was confirmed by running it, not inferred from docs. If a fact
turns out to be wrong, fix it here in the same change that works around it.

---

## RocketRide

### 1. The API host is `api.rocketride.ai`

`https://cloud.rocketride.ai` is the **Studio web UI**. It answers the `wss://`
upgrade with `HTTP 200`, so the SDK fails with:

```
ConnectionError: server rejected WebSocket connection: HTTP 200
```

Use `https://api.rocketride.ai` — also the SDK's own `CONST_DEFAULT_WEB_CLOUD`.
The client normalizes it to `wss://api.rocketride.ai/task/service`.

### 2. `llm_*` nodes attach to an agent as a control resource — they are not data-lane nodes

Across all four workshop `.pipe` files, an `llm_anthropic` node **never** has an
`input` lane. It is always wired as:

```json
"control": [{ "classType": "llm", "from": "<agent_id>" }]
```

So there is no `webhook → llm → response` chain. The minimum shape for a single
Anthropic call is a **tool-less `agent_deepagent`** with an `llm_anthropic`
control-attached:

```
webhook_1 --text--> question_1 --questions--> agent_min --answers--> response_answers_1
                                                 ^
                                                 | control: llm
                                           llm_anthropic_min
```

Tools attach the same way, with `"classType": "tool"`; subagents with
`"classType": "deepagent"`.

Response shape from `send()` is wrapped three layers deep — `{"answers": [...]}`,
whose `answers[0]` is a **string** holding a JSON array of Anthropic content
blocks, whose `text` block holds `{"type":"final","content":"<answer>"}`.
See `extract_answer()` in `smoke_rr.py`.

### 3. Only `ROCKETRIDE_`-prefixed vars reach the engine

`client.use(env={...})` forwards variables to the engine for `${...}`
substitution inside the `.pipe`, but the client's own `.env`-derived
environment is filtered to keys starting with `ROCKETRIDE_`
(`mixins/execution.py`). So an Anthropic key must be passed under a prefixed
name:

```python
await client.use(..., env={"ROCKETRIDE_ANTHROPIC_KEY": anthropic_key})
```

This is what keeps secrets out of git: the `.pipe` stores the literal string
`${ROCKETRIDE_ANTHROPIC_KEY}` and the value is substituted server-side.

Also note the SDK auto-reads **`ROCKETRIDE_APIKEY`** (no underscore) from
`.env`, while this project's `.env` uses **`ROCKETRIDE_API_KEY`**. The names do
not match, so the auto-pickup never fires — always pass `auth=` explicitly.

### 4. This machine's Python has no CA bundle — certifi is required

The python.org macOS build ships no CA certificates (`ssl`'s `openssl_cafile`
path does not exist), so TLS fails with:

```
ssl.SSLCertVerificationError: [SSL: CERTIFICATE_VERIFY_FAILED] unable to get local issuer certificate
```

`smoke_rr.py:ensure_ca_bundle()` sets `SSL_CERT_FILE` to `certifi.where()` when
the interpreter has no bundle of its own. Process-scoped; a caller-set
`SSL_CERT_FILE` still wins. The system-wide alternative is
`/Applications/Python 3.14/Install Certificates.command`, deliberately not used.

`certifi` is therefore a real dependency even though nothing imports it
transitively — it is pinned in `requirements.txt`.

### 8. Topological fan-out does NOT parallelize; asyncio.gather does (measured)

A `.pipe` with one `question` node fanning out to N `agent_deepagent`
branches, fanning back into one `response_answers`, executes those branches
**sequentially**. Measured against RocketRide Cloud with `loadtest.pipe`:

| Run                         | Wall clock |
| --------------------------- | ---------- |
| serial, 1 branch (p1)       | 21.4 s     |
| serial, 1 branch (p2)       | 20.5 s     |
| serial total, N=2           | 41.9 s     |
| **parallel pipe, N=2**      | **46.0 s** |
| parallel pipe, N=2, threads=4 | 46.2 s   |
| **parallel pipe, N=3**      | **66.8 s** |

Wall clock scales linearly with N (~21 s per branch), and the N-branch pipe is
slightly *slower* than running the branches one at a time — so the fan-out buys
nothing. `use(threads=4)` makes no difference.

This contradicts the docs' execution-model claim that "independent branches run
concurrently across threads" — that may hold for streaming data nodes but not
for `agent_deepagent` branches on this deployment. Unresolved; do not assume
topological fan-out gives concurrency without measuring.

**What actually works: concurrency from the client.** Running N single-branch
pipelines through `asyncio.gather` (N separate `use()`/`send()` calls, one
client each, fan-in in Python) is flat in N:

| mode                        | N=2     | N=3     |
| --------------------------- | ------- | ------- |
| serial (one after another)  | 59.2 s  | 86.2 s  |
| A: one N-branch pipe        | 47.6 s  | 63.4 s  |
| **B: asyncio.gather**       | **32.7 s** | **33.4 s** |

B's wall clock is roughly the slowest single branch and barely moves from N=2 to
N=3 -- real N-way concurrency. A still grows with N. Use B. Measured by
`bench.py`; every run is appended to the Hotdata telemetry database.

A dedicated `llm_anthropic` node per branch was never the issue -- `gen_pipe.py`
has always emitted one per branch (1:1, verified), and A is still serialized.

**Each pipe needs its own `project_id`.** The engine keys a running pipeline by
`project_id`; N pipes sharing one id cannot run concurrently and the second
`use()` fails with `Pipeline is already running.` `gen_pipe.py` derives a
per-pipe id with `uuid5`.

### 9. No agent tool executes on RocketRide Cloud (unresolved)

An `agent_deepagent` discovers its tools but never runs them. The agent's final
answer is the tool call itself, emitted onto the answers lane:

```
{"type":"tool_call","name":"<nodeId>.http_request","args":{...}}
```

Runtime flow events (`pipelineTraceLevel="full"`, `set_events([...,"flow",...])`)
show where it stops -- `tool.query` fires once and returns the tool descriptor,
then the LLM is asked three times, and no `tool.execute` ever reaches the node:

```
seq 21 enter tool_http_probe      invoke=tool op=tool.query   <- discovery
seq 22 leave tool_http_probe      invoke=tool op=tool.query   <- returns the descriptor
seq 26 enter llm_anthropic_probe  invoke=llm  op=ask
seq 38 enter llm_anthropic_probe  invoke=llm  op=ask          <- 3 LLM calls
seq 49 enter llm_anthropic_probe  invoke=llm  op=ask
seq 61 enter response_answers_1                               <- tool_call goes out as the answer
```

**This is not specific to `tool_http_request`.** Copying the workshop's
`agent_deepagent` + `llm_anthropic` config verbatim and swapping in
`tool_python` gives byte-identical behaviour: `tool.query` only, no
`tool.execute`. `client.validate()` reports the pipeline valid. Rewriting the
system prompt changes nothing.

**`tool_shell` does not exist on Cloud at all.** It appears in `get_services()`
but with `plans`, `capabilities` and `actions` all `null` -- a stub. `use()`
rejects it:

```
RuntimeError: The service tool_shell was not found
```

So the workshop's coding-agent pipelines cannot run on Cloud as written; they
assume a local engine. Do not build on agent tool use here until this is
understood.

The Hotdata HTTP contract itself is confirmed working by curl:

```
POST https://api.hotdata.dev/v1/query
Authorization: Bearer <HOTDATA_API_KEY>
X-Workspace-Id: <workspace id>
X-Database-Id:  <database id>
{"sql": "SELECT SUM(x) AS total FROM smoketest3.public.t"}   -> 200, rows [[6]]
```

### 10. Two traps when handing an agent a credential

**The agent will echo it.** `${...}` substitution works inside `system_prompt`
text, not just config fields, so a key placed there reaches the LLM -- and the
agent repeated the Hotdata bearer token verbatim in its reply. Never print an
agent answer without masking known secret values first
(`redact()` in `smoke_hotdata.py`).

**The URL whitelist cannot be set at all on this deployment.** Every shape is
ignored -- escaped+anchored array, plain-host array, and nested under a
`profile` key. The engine always warns:

```
Warning*URL whitelist is empty - all URLs will be allowed*/opt/rocketride/nodes/tool_http_request/IGlobal.py:137
```

The docs' scalar `whitelistPattern` does not help: the **deployed** node's
schema (`get_services()["tool_http_request"]["Pipe"]["schema"]`) has
`urlWhitelist` and no `whitelistPattern` or `serverName` at all, so
docs.rocketride.org describes a newer build than Cloud runs. Treat the HTTP tool
as unrestricted; do not rely on the whitelist as a guardrail.

---

## Telemetry

Benchmark and probe results go to the Hotdata database `loadtest_telemetry`
(id `dbidpeq7ewvmsqwtn1n1z20lix82yy`, created with `--expires-at 3d` per fact 7):

- `loadtest_telemetry.public.runs` -- one row per benchmark run, `mode` column
  is `serial` / `A` / `B`. Written by `telemetry.record()` from `bench.py`.
- `loadtest_telemetry.public.findings` -- one row per C/D probe result.

Instant databases reject `INSERT`/DDL over the query API, so rows are staged to
a local JSON file and loaded with
`hotdata databases load --catalog <catalog> --table <table> --append`.

---

## Hotdata

### 5. Use the standard API Token, not the Database API Token

The **standard API Token** can create and delete databases. The **Database API
Token cannot delete databases**. Use the standard token for any workflow that
tears down what it creates.

### 6. CLI 0.33 flag shapes

- **Load:** `--catalog <name>` — takes the catalog *name*.
- **Delete a database:** `remove <ID>` — takes the *ID*, not the name.

### 7. Always set `--expires-at` when creating a database

So load-test scratch databases self-clean:

| Database kind | TTL  |
| ------------- | ---- |
| persona       | 1h   |
| telemetry     | 3d   |
