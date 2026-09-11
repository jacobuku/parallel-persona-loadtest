"""Run the persona load test and report wall-clock time.

Two modes, same N branches either way:

  parallel  one N-branch pipe; the engine runs independent branches
            concurrently across threads. One use() + one send().
  serial    N single-branch pipes, each started, sent, and terminated on
            its own, one after another. Sum of the parts.

Usage:
    python run_loadtest.py --n 2                 # parallel
    python run_loadtest.py --n 2 --mode serial
    python run_loadtest.py --n 2 --mode both     # runs both, prints speedup
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import ssl
import sys
import time
from pathlib import Path
from typing import Any

import certifi
from dotenv import load_dotenv
from rocketride import RocketRideClient

ROOT = Path(__file__).resolve().parent
PIPELINES_DIR = ROOT / "pipelines"
WEBHOOK_SOURCE_ID = "webhook_1"
ANTHROPIC_PIPE_VAR = "ROCKETRIDE_ANTHROPIC_KEY"
DEFAULT_URI = "https://api.rocketride.ai"

# The webhook payload. Each branch's own persona first_message is already baked
# into its system prompt, so this is just the trigger that starts every branch.
TRIGGER = "A guest has just messaged the front desk. Reply to them."


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


def answer_texts(result: Any) -> list[str]:
    """Return every branch answer, unwrapping the three-layer response shape.

    `{"answers": [...]}` -> each entry a JSON string of Anthropic content
    blocks -> the `text` block holds `{"type":"final","content":...}`.
    """
    answers: Any = result
    if isinstance(result, dict):
        answers = result.get("answers")
        if answers is None and isinstance(result.get("result"), dict):
            answers = result["result"].get("answers")
    if isinstance(answers, str):
        answers = [answers]
    if not isinstance(answers, list):
        return []

    out: list[str] = []
    for raw in answers:
        if isinstance(raw, dict):
            raw = raw.get("text") or raw.get("content") or ""
        if not isinstance(raw, str) or not raw.strip():
            continue
        text = raw.strip()
        try:
            blocks = json.loads(text)
        except (ValueError, TypeError):
            out.append(text)
            continue
        if isinstance(blocks, list):
            parts = [
                b["text"] for b in blocks
                if isinstance(b, dict) and b.get("type") == "text" and isinstance(b.get("text"), str)
            ]
            text = "\n".join(p for p in parts if p.strip()).strip() or text
        try:
            payload = json.loads(text)
            if isinstance(payload, dict) and isinstance(payload.get("content"), str):
                text = payload["content"].strip()
        except (ValueError, TypeError):
            pass
        out.append(text)
    return out


async def run_one(
    client: RocketRideClient,
    pipe: Path,
    env: dict[str, str],
    threads: int | None = None,
) -> tuple[float, list[str]]:
    """use -> send -> terminate one pipe. Returns (seconds, answers)."""
    started = time.perf_counter()
    token: str | None = None
    try:
        kwargs: dict[str, Any] = {}
        if threads is not None:
            kwargs["threads"] = threads
        result = await client.use(
            filepath=str(pipe), source=WEBHOOK_SOURCE_ID, ttl=0, env=env, **kwargs
        )
        token = result["token"]
        response = await client.send(
            token=token,
            data=TRIGGER.encode("utf-8"),
            mimetype="text/plain",
            objinfo={"mimetype": "text/plain"},
        )
        return time.perf_counter() - started, answer_texts(response)
    finally:
        if token is not None:
            await client.terminate(token)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=2)
    ap.add_argument("--mode", choices=["parallel", "serial", "both"], default="parallel")
    ap.add_argument("--show-answers", action="store_true")
    ap.add_argument("--threads", type=int, default=None,
                    help="engine threads for the parallel pipe (default: server decides)")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    ensure_ca_bundle()
    env = {ANTHROPIC_PIPE_VAR: require_env("ANTHROPIC_API_KEY")}
    uri = os.environ.get("ROCKETRIDE_URI", DEFAULT_URI)

    parallel_pipe = PIPELINES_DIR / "loadtest.pipe"
    serial_pipes = sorted((PIPELINES_DIR / "serial").glob("loadtest-*.pipe"))[: args.n]

    if args.mode in ("parallel", "both") and not parallel_pipe.exists():
        sys.exit(f"[fatal] {parallel_pipe} missing -- run: python gen_pipe.py --n {args.n}")
    if args.mode in ("serial", "both") and len(serial_pipes) < args.n:
        sys.exit(f"[fatal] need {args.n} serial pipes -- run: python gen_pipe.py --n {args.n} --serial")

    client = RocketRideClient(uri=uri, auth=require_env("ROCKETRIDE_API_KEY"))
    await client.connect()
    print(f"connected to {uri} (N={args.n})\n")

    timings: dict[str, float] = {}
    try:
        if args.mode in ("parallel", "both"):
            tlabel = f", threads={args.threads}" if args.threads else ""
            print(f"[parallel] one {args.n}-branch pipe{tlabel} ...")
            elapsed, answers = await run_one(client, parallel_pipe, env, threads=args.threads)
            timings["parallel"] = elapsed
            print(f"[parallel] {elapsed:.2f}s wall clock, {len(answers)} answer(s)")
            if args.show_answers:
                for i, a in enumerate(answers, 1):
                    print(f"    --- branch {i} ---\n    {a}\n")
            print()

        if args.mode in ("serial", "both"):
            print(f"[serial] {args.n} single-branch pipes, one at a time ...")
            total = 0.0
            for pipe in serial_pipes:
                elapsed, answers = await run_one(client, pipe, env)
                total += elapsed
                print(f"[serial]   {pipe.name}: {elapsed:.2f}s, {len(answers)} answer(s)")
                if args.show_answers:
                    for a in answers:
                        print(f"      {a}\n")
            timings["serial"] = total
            print(f"[serial] {total:.2f}s wall clock total\n")
    finally:
        await client.disconnect()

    if len(timings) == 2:
        p, s = timings["parallel"], timings["serial"]
        print("=" * 52)
        print(f"  parallel : {p:6.2f}s")
        print(f"  serial   : {s:6.2f}s")
        print(f"  speedup  : {s / p:6.2f}x  ({s - p:+.2f}s saved)")
        print("=" * 52)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
