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

### 8. Topological fan-out does NOT run agent branches concurrently (measured)

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

Per the docs, the parallelism that *is* documented to work is **within** one
agent: `agent_rocketride` runs a wave of tool calls concurrently (max 8
threads), and any agent's multiple independent tool calls in one reasoning step
are fanned out automatically.

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
