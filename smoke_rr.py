"""Smoke test: run one text turn through the minimal RocketRide Cloud pipeline.

Reads ROCKETRIDE_API_KEY and ANTHROPIC_API_KEY from .env via python-dotenv.
Neither key is ever printed.

The pipeline (pipelines/minimal-llm.pipe) is:

    webhook_1 --text--> question_1 --questions--> agent_min --answers--> response_answers_1
                                                     ^
                                                     | control: llm
                                               llm_anthropic_min

`llm_anthropic` is a control-attached resource, not a data-lane node (true of
every llm node in the workshop pipes), so the Anthropic call is driven by a
tool-less `agent_deepagent` -- the minimal shape those pipes prove out.

The .pipe holds `${ROCKETRIDE_ANTHROPIC_KEY}`, not the key itself, so the file
is safe to commit. The real value is substituted server-side from the `env`
dict passed to `use()`.

Run:  .venv/bin/python smoke_rr.py
"""

from __future__ import annotations

import asyncio
import json
import os
import ssl
import sys
from pathlib import Path
from typing import Any

import certifi
from dotenv import load_dotenv
from rocketride import RocketRideClient

ROOT = Path(__file__).resolve().parent
PIPELINE_PATH = ROOT / "pipelines" / "minimal-llm.pipe"

# Source node ID inside minimal-llm.pipe. Must match the JSON.
WEBHOOK_SOURCE_ID = "webhook_1"

# `use(env=...)` only forwards ROCKETRIDE_*-prefixed vars to the engine for
# `${...}` substitution, so the Anthropic key is re-exported under this name.
ANTHROPIC_PIPE_VAR = "ROCKETRIDE_ANTHROPIC_KEY"

# The SDK's own CONST_DEFAULT_WEB_CLOUD. Note that https://cloud.rocketride.ai
# is the Studio web UI -- it answers the wss:// upgrade with HTTP 200, so it is
# NOT the API host. Override with ROCKETRIDE_URI if the endpoint moves.
DEFAULT_URI = "https://api.rocketride.ai"

PROMPT = "In one sentence: what is a data pipeline?"


def ensure_ca_bundle() -> None:
    """Point OpenSSL at certifi's CA bundle when the interpreter has none.

    The python.org macOS build ships no CA bundle of its own (its
    `openssl_cafile` path does not exist until you run "Install
    Certificates.command"), so the wss:// handshake to RocketRide Cloud fails
    with CERTIFICATE_VERIFY_FAILED. Setting SSL_CERT_FILE fixes it for this
    process only -- no system-wide change. An SSL_CERT_FILE the caller already
    set always wins.
    """
    if os.environ.get("SSL_CERT_FILE"):
        return
    if os.path.exists(ssl.get_default_verify_paths().openssl_cafile or ""):
        return
    os.environ["SSL_CERT_FILE"] = certifi.where()


def require_env(name: str) -> str:
    """Fetch a required env var, erroring out without ever echoing the value."""
    value = os.environ.get(name, "").strip()
    if not value:
        sys.exit(f"[fatal] {name} is missing or empty in .env")
    return value


def _unwrap_final(text: str) -> str:
    """Unwrap the agent's `{"type":"final","content":"..."}` envelope.

    Returned as-is when the text is not that envelope.
    """
    try:
        payload = json.loads(text)
    except (ValueError, TypeError):
        return text
    if isinstance(payload, dict):
        content = payload.get("content")
        if isinstance(content, str) and content.strip():
            return content.strip()
    return text


def _text_from_blocks(blocks: list[Any]) -> str:
    """Pull the assistant text out of a list of Anthropic content blocks.

    Skips `thinking` blocks; keeps the `text` ones in order.
    """
    parts = [
        b["text"]
        for b in blocks
        if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
    ]
    return _unwrap_final("\n".join(p for p in parts if p.strip()).strip())


def extract_answer(result: Any) -> str:
    """Dig the human-readable answer out of a pipeline result.

    The response arrives wrapped in three layers, each unwrapped defensively so
    a shape change degrades to the raw payload instead of crashing:

      1. `{"answers": [...]}`  (or the nested `{"result": {"answers": [...]}}`)
      2. answers[0] is a *string* holding a JSON array of content blocks
      3. the `text` block holds `{"type":"final","content":"<answer>"}`
    """
    answers: Any = result
    if isinstance(result, dict):
        answers = result.get("answers")
        if answers is None and isinstance(result.get("result"), dict):
            answers = result["result"].get("answers")

    if isinstance(answers, str):
        answers = [answers]
    if not isinstance(answers, list):
        return ""

    for answer in answers:
        if isinstance(answer, dict):
            answer = answer.get("text") or answer.get("content") or ""
        if not isinstance(answer, str) or not answer.strip():
            continue

        # Layer 2: the string is usually a JSON array of content blocks.
        try:
            blocks = json.loads(answer)
        except (ValueError, TypeError):
            return _unwrap_final(answer.strip())

        if isinstance(blocks, list):
            text = _text_from_blocks(blocks)
            if text:
                return text
        return _unwrap_final(answer.strip())

    return ""


async def main() -> int:
    load_dotenv(ROOT / ".env")
    ensure_ca_bundle()

    rocketride_key = require_env("ROCKETRIDE_API_KEY")
    anthropic_key = require_env("ANTHROPIC_API_KEY")
    uri = os.environ.get("ROCKETRIDE_URI", DEFAULT_URI)

    print(f"[1/5] connecting to {uri} ...")
    client = RocketRideClient(uri=uri, auth=rocketride_key)
    await client.connect()
    print(f"      connected (authenticated={client.is_authenticated()})")

    token: str | None = None
    exit_code = 0
    try:
        print(f"[2/5] loading pipeline {PIPELINE_PATH.name} (source={WEBHOOK_SOURCE_ID}) ...")
        result = await client.use(
            filepath=str(PIPELINE_PATH),
            source=WEBHOOK_SOURCE_ID,
            # ttl=0 disables idle-pipeline GC so a slow first LLM call can't
            # collect the pipeline out from under us.
            ttl=0,
            env={ANTHROPIC_PIPE_VAR: anthropic_key},
        )
        token = result["token"]
        print("      pipeline running")

        print(f"[3/5] sending prompt: {PROMPT!r}")
        response = await client.send(
            token=token,
            data=PROMPT.encode("utf-8"),
            mimetype="text/plain",
            objinfo={"mimetype": "text/plain"},
        )

        answer = extract_answer(response)
        if answer:
            print("[4/5] pipeline output:")
            print()
            print(answer)
            print()
        else:
            print("[4/5] no answer text found; raw response follows:")
            print(f"      {response!r}")
            exit_code = 1
    finally:
        if token is not None:
            print("[5/5] terminating pipeline ...")
            await client.terminate(token)
        await client.disconnect()
        print("      done")

    return exit_code


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
