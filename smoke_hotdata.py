"""Verify an agent can query its own Hotdata database via tool_http_request.

Generates `pipelines/hotdata-probe.pipe` and runs one turn through it.

    webhook_1 -> question_1 -> agent_hotdata -> response_answers_1
                                   ^   ^
                          control: |   | control: tool
                            llm_anthropic  tool_http_request

The database id is NOT baked into the pipe. It arrives as ${ROCKETRIDE_DB_P1}
and is substituted server-side from `use(env=...)`, same mechanism as the
Anthropic key (NOTES.md fact 3). The workshop pipes prove `${...}` substitution
works inside system_prompt text, not just config fields.

Hotdata HTTP contract, confirmed by curl before wiring it here:

    POST https://api.hotdata.dev/v1/query
    Authorization: Bearer <api key>
    X-Workspace-Id: <workspace id>
    X-Database-Id:  <database id>
    {"sql": "..."}
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import ssl
import sys
from pathlib import Path
from typing import Any

import certifi
from dotenv import load_dotenv
from rocketride import RocketRideClient

ROOT = Path(__file__).resolve().parent
PIPE_PATH = ROOT / "pipelines" / "hotdata-probe.pipe"
WEBHOOK_SOURCE_ID = "webhook_1"
DEFAULT_URI = "https://api.rocketride.ai"
LLM_PROFILE = "claude-sonnet-4-6"

# From the hotdata CLI config; override via .env if they change.
DEFAULT_DB = "dbidnoqi1blgvnpbe3srb41f8ds3jy"
DEFAULT_WS = "work9z04fz71945u9zjy1y586l5nuw"

HOTDATA_HOST = "api.hotdata.dev"
# Literal host + explicit boundary, the form the node's whitelist grammar accepts.
# Set via the scalar `whitelistPattern`, NOT the `urlWhitelist` array: with the
# array form the engine still warned "URL whitelist is empty - all URLs will be
# allowed", i.e. it was ignored. See NOTES.md fact 10.
WHITELIST_PATTERN = r"^https://api\.hotdata\.dev(?::[0-9]+)?(?:/|$)"

# Only the database knows this: smoketest3.public.t holds x = 1, 2, 3 -> 6.
ASK = (
    "Query the database and tell me the total. Reply with just the number and "
    "one short sentence saying where it came from."
)

SYSTEM_PROMPT = f"""You are a database probe agent. You answer questions by querying a Hotdata database over HTTP.

=== Your tool ===

You have one tool, registered as `tool_http_probe.http_request`. CALL IT. Do not
describe the call, do not print the call as JSON, do not plan -- actually invoke
the tool and wait for its response.

Use these arguments:

- method: POST
- url: https://{HOTDATA_HOST}/v1/query
- bearer_token: ${{ROCKETRIDE_HOTDATA_KEY}}
- headers: X-Workspace-Id = ${{ROCKETRIDE_HOTDATA_WS}}, X-Database-Id = ${{ROCKETRIDE_DB_P1}}
- body_json: an object whose `sql` key is: SELECT SUM(x) AS total FROM smoketest3.public.t

The tool returns JSON shaped like {{"columns": ["total"], "rows": [[6]]}}. The
number you want is at rows[0][0].

=== Rules ===

- Invoke the tool exactly once, then answer from what it returned.
- NEVER repeat the bearer token, any header value, or any credential in your
  reply. Report only the number.
- Do not invent a number. If the call fails, report the HTTP status and the
  error body, with any credential removed.
- Reply with plain text only -- no JSON, no preamble.
"""


def ensure_ca_bundle() -> None:
    """python.org macOS Python ships no CA bundle; fall back to certifi. See NOTES.md."""
    if os.environ.get("SSL_CERT_FILE"):
        return
    if os.path.exists(ssl.get_default_verify_paths().openssl_cafile or ""):
        return
    os.environ["SSL_CERT_FILE"] = certifi.where()


def require_env(name: str) -> str:
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"[fatal] {name} is missing or empty in .env")
    return value


def build_pipe() -> dict[str, Any]:
    return {
        "components": [
            {
                "id": "webhook_1", "provider": "webhook", "name": "Web Hook",
                "config": {"hideForm": True, "mode": "Source", "parameters": {},
                           "type": "webhook", "name": "Web Hook"},
                "ui": {"position": {"x": 50, "y": 200}, "nodeType": "default",
                       "formDataValid": True},
            },
            {
                "id": "question_1", "provider": "question", "name": "Question",
                "config": {"type": "question", "name": "Question"},
                "ui": {"position": {"x": 320, "y": 200}, "nodeType": "default",
                       "formDataValid": True},
                "input": [{"lane": "text", "from": "webhook_1"}],
            },
            {
                "id": "agent_hotdata", "provider": "agent_deepagent", "name": "Hotdata Probe",
                "config": {
                    "profile": "default",
                    "default": {
                        "advanced_mode": True,
                        "agent_description": "Queries a Hotdata database over HTTP and reports the result.",
                        "system_prompt": SYSTEM_PROMPT,
                    },
                    "name": "Hotdata Probe",
                },
                "ui": {"position": {"x": 620, "y": 200}, "nodeType": "default",
                       "formDataValid": True},
                "input": [{"lane": "questions", "from": "question_1"}],
            },
            {
                "id": "llm_anthropic_probe", "provider": "llm_anthropic", "name": "Anthropic",
                "config": {
                    "profile": LLM_PROFILE,
                    LLM_PROFILE: {"modelSource": "manual", "apikey": "${ROCKETRIDE_ANTHROPIC_KEY}"},
                    "name": "Anthropic",
                },
                "ui": {"position": {"x": 560, "y": 380}, "nodeType": "default",
                       "formDataValid": True},
                "control": [{"classType": "llm", "from": "agent_hotdata"}],
            },
            {
                "id": "tool_http_probe", "provider": "tool_http_request", "name": "HTTP",
                "config": {
                    "type": "tool_http_request",
                    "serverName": "http",
                    "allowGET": True, "allowPOST": True,
                    "allowPUT": False, "allowPATCH": False, "allowDELETE": False,
                    "allowHEAD": False, "allowOPTIONS": False,
                    "whitelistPattern": WHITELIST_PATTERN,
                    "rateLimitPerSecond": 10,
                    "rateLimitPerMinute": 100,
                    "maxConcurrentRequests": 5,
                    "name": "HTTP",
                },
                "ui": {"position": {"x": 700, "y": 380}, "nodeType": "default",
                       "formDataValid": True},
                "control": [{"classType": "tool", "from": "agent_hotdata"}],
            },
            {
                "id": "response_answers_1", "provider": "response_answers", "name": "Return Answers",
                "config": {"laneName": "answers", "name": "Return Answers"},
                "ui": {"position": {"x": 960, "y": 200}, "nodeType": "default",
                       "formDataValid": True},
                "input": [{"lane": "answers", "from": "agent_hotdata"}],
            },
        ],
        "project_id": "5a1d9e73-2b48-4c6f-8e90-71fd3a0c6b25",
        "version": 1, "isLocked": False, "snapToGrid": True,
        "snapGridSize": [10, 10], "docRevision": 1,
    }


def redact(text: str, secrets: dict[str, str]) -> str:
    """Mask any secret value that appears in text before it is printed.

    The agent is handed the Hotdata key inside its system prompt, so it can
    echo the key back in its reply. Never print an agent answer un-redacted.
    """
    for label, value in secrets.items():
        if value and len(value) > 6:
            text = text.replace(value, f"<{label}:REDACTED>")
    return text


def answer_text(result: Any) -> str:
    answers: Any = result
    if isinstance(result, dict):
        answers = result.get("answers")
        if answers is None and isinstance(result.get("result"), dict):
            answers = result["result"].get("answers")
    if isinstance(answers, str):
        answers = [answers]
    for raw in answers or []:
        if isinstance(raw, dict):
            raw = raw.get("text") or raw.get("content") or ""
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = raw.strip()
        try:
            blocks = json.loads(text)
            if isinstance(blocks, list):
                parts = [b["text"] for b in blocks if isinstance(b, dict)
                         and b.get("type") == "text" and isinstance(b.get("text"), str)]
                text = "\n".join(p for p in parts if p.strip()).strip() or text
        except (ValueError, TypeError):
            pass
        try:
            payload = json.loads(text)
            if isinstance(payload, dict) and isinstance(payload.get("content"), str):
                text = payload["content"].strip()
        except (ValueError, TypeError):
            pass
        return text
    return ""


async def main() -> int:
    load_dotenv(ROOT / ".env")
    ensure_ca_bundle()

    db_id = os.environ.get("HOTDATA_DATABASE_ID", "").strip() or DEFAULT_DB
    ws_id = os.environ.get("HOTDATA_WORKSPACE_ID_OVERRIDE", "").strip() or DEFAULT_WS

    PIPE_PATH.parent.mkdir(parents=True, exist_ok=True)
    PIPE_PATH.write_text(json.dumps(build_pipe(), indent=2) + "\n", encoding="utf-8")
    print(f"wrote {PIPE_PATH.relative_to(ROOT)}")
    print(f"database id -> ROCKETRIDE_DB_P1 ({db_id[:8]}...), workspace -> ROCKETRIDE_HOTDATA_WS")

    env = {
        "ROCKETRIDE_ANTHROPIC_KEY": require_env("ANTHROPIC_API_KEY"),
        "ROCKETRIDE_HOTDATA_KEY": require_env("HOTDATA_API_KEY"),
        "ROCKETRIDE_HOTDATA_WS": ws_id,
        "ROCKETRIDE_DB_P1": db_id,
    }

    uri = os.environ.get("ROCKETRIDE_URI", DEFAULT_URI)
    client = RocketRideClient(uri=uri, auth=require_env("ROCKETRIDE_API_KEY"))
    await client.connect()
    print(f"connected to {uri}")

    token: str | None = None
    try:
        result = await client.use(filepath=str(PIPE_PATH), source=WEBHOOK_SOURCE_ID,
                                  ttl=0, env=env)
        token = result["token"]
        print("pipeline running; asking the agent to query the database ...")
        response = await client.send(
            token=token, data=ASK.encode("utf-8"),
            mimetype="text/plain", objinfo={"mimetype": "text/plain"},
        )
        answer = answer_text(response)
        secrets = {
            "HOTDATA_KEY": env["ROCKETRIDE_HOTDATA_KEY"],
            "ANTHROPIC_KEY": env["ROCKETRIDE_ANTHROPIC_KEY"],
            "ROCKETRIDE_KEY": os.environ.get("ROCKETRIDE_API_KEY", ""),
        }
        safe = redact(answer or repr(response), secrets)
        print("\n--- agent answer (secrets redacted) ---")
        print(safe)
        print("---------------------------------------")

        # A real pass means the agent REPORTED the total, not that it echoed a
        # tool_call plan back at us. Reject any answer that is still a tool call,
        # and look for 6 as a standalone number rather than a substring of an id.
        emitted_tool_call = '"type":"tool_call"' in (answer or "").replace(" ", "")
        found_total = bool(re.search(r"(?<![0-9a-zA-Z])6(?![0-9a-zA-Z])", answer or ""))
        ok = found_total and not emitted_tool_call
        if emitted_tool_call:
            print("\n[FAIL] the agent returned a tool_call as its final answer -- "
                  "the tool was never executed.")
        print(f"expected SUM(x)=6 from smoketest3.public.t -> {'PASS' if ok else 'FAIL'}")
        return 0 if ok else 1
    finally:
        if token is not None:
            await client.terminate(token)
        await client.disconnect()


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
