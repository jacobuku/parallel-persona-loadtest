"""Benchmark three ways of running N persona branches, and record each result.

Modes (the `mode` column in the telemetry table):

  serial   N single-branch pipes, one after another. The baseline.
  A        one N-branch pipe: topological fan-out inside the engine. Each
           branch already has its OWN llm_anthropic node (1:1, not shared).
  B        N single-branch pipes started concurrently from Python with
           asyncio.gather -- N separate use()+send() calls, fan-in in Python.

Every run is appended to the Hotdata telemetry database via telemetry.record().

Usage:
    python bench.py --modes serial A B --ns 2 3
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

import telemetry
from run_loadtest import TRIGGER, WEBHOOK_SOURCE_ID, answer_texts, ensure_ca_bundle, require_env

ROOT = Path(__file__).resolve().parent
PIPELINES_DIR = ROOT / "pipelines"
DEFAULT_URI = "https://api.rocketride.ai"


async def new_client(uri: str, key: str) -> RocketRideClient:
    c = RocketRideClient(uri=uri, auth=key)
    await c.connect()
    return c


async def run_pipe(client: RocketRideClient, pipe: Path, env: dict[str, str]) -> tuple[float, int]:
    """use -> send -> terminate. Returns (seconds, answer count)."""
    started = time.perf_counter()
    token: str | None = None
    try:
        result = await client.use(filepath=str(pipe), source=WEBHOOK_SOURCE_ID, ttl=0, env=env)
        token = result["token"]
        response = await client.send(
            token=token, data=TRIGGER.encode("utf-8"),
            mimetype="text/plain", objinfo={"mimetype": "text/plain"},
        )
        return time.perf_counter() - started, len(answer_texts(response))
    finally:
        if token is not None:
            await client.terminate(token)


async def mode_serial(uri, key, env, pipes) -> tuple[float, int, list[float]]:
    client = await new_client(uri, key)
    per: list[float] = []
    answers = 0
    started = time.perf_counter()
    try:
        for p in pipes:
            secs, n = await run_pipe(client, p, env)
            per.append(secs)
            answers += n
    finally:
        await client.disconnect()
    return time.perf_counter() - started, answers, per


async def mode_a(uri, key, env, pipe) -> tuple[float, int, list[float]]:
    client = await new_client(uri, key)
    try:
        started = time.perf_counter()
        secs, n = await run_pipe(client, pipe, env)
        return time.perf_counter() - started, n, [secs]
    finally:
        await client.disconnect()


async def mode_b(uri, key, env, pipes) -> tuple[float, int, list[float]]:
    """N concurrent single-branch pipelines, one client each.

    A client per task so nothing serializes on a shared WebSocket -- this is
    meant to measure the engine, not the SDK's request queue.
    """
    clients = await asyncio.gather(*(new_client(uri, key) for _ in pipes))
    try:
        started = time.perf_counter()
        results = await asyncio.gather(
            *(run_pipe(c, p, env) for c, p in zip(clients, pipes))
        )
        wall = time.perf_counter() - started
        return wall, sum(n for _, n in results), [s for s, _ in results]
    finally:
        await asyncio.gather(*(c.disconnect() for c in clients), return_exceptions=True)


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--modes", nargs="+", default=["serial", "A", "B"],
                    choices=["serial", "A", "B"])
    ap.add_argument("--ns", nargs="+", type=int, default=[2, 3])
    ap.add_argument("--no-telemetry", action="store_true")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    ensure_ca_bundle()
    uri = os.environ.get("ROCKETRIDE_URI", DEFAULT_URI)
    key = require_env("ROCKETRIDE_API_KEY")
    env = {"ROCKETRIDE_ANTHROPIC_KEY": require_env("ANTHROPIC_API_KEY")}

    rows: list[dict[str, Any]] = []
    for n in args.ns:
        # Regenerate both shapes for this N.
        os.system(f"{sys.executable} gen_pipe.py --n {n} >/dev/null")
        os.system(f"{sys.executable} gen_pipe.py --n {n} --serial >/dev/null")
        parallel_pipe = PIPELINES_DIR / "loadtest.pipe"
        serial_pipes = sorted((PIPELINES_DIR / "serial" / "v1").glob("loadtest-*.pipe"))[:n]

        for mode in args.modes:
            print(f"\n=== mode={mode} N={n} ===")
            if mode == "serial":
                wall, answers, per = await mode_serial(uri, key, env, serial_pipes)
            elif mode == "A":
                wall, answers, per = await mode_a(uri, key, env, parallel_pipe)
            else:
                wall, answers, per = await mode_b(uri, key, env, serial_pipes)

            per_str = ", ".join(f"{p:.2f}" for p in per)
            print(f"  wall={wall:.2f}s answers={answers} branches=[{per_str}]")
            rows.append({
                "ts": telemetry.utc_now(),
                "mode": mode,
                "n": n,
                "wall_clock_s": round(wall, 3),
                "answers": answers,
                "branch_secs": per_str,
                "max_branch_s": round(max(per), 3) if per else None,
                "sum_branch_s": round(sum(per), 3) if per else None,
                "uri": uri,
            })

    print("\n" + "=" * 64)
    print(f"{'mode':<8}{'N':>3}{'wall(s)':>10}{'answers':>9}   branches")
    for r in rows:
        print(f"{r['mode']:<8}{r['n']:>3}{r['wall_clock_s']:>10.2f}{r['answers']:>9}   [{r['branch_secs']}]")
    print("=" * 64)

    if not args.no_telemetry:
        telemetry.record(rows)
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
