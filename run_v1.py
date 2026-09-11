"""v1 persona run: N concurrent single-branch pipelines, graded, with telemetry.

Architecture B (NOTES.md fact 8): N separate `use()`/`send()` calls started
together with asyncio.gather, one RocketRide client each, fan-in in Python.
Topological fan-out inside one pipe does not parallelise on this deployment;
client-side gather does.

Per persona, one turn is:

    Hotdata: create db -> load pricing/availability/venue_facts
             -> query the rows this persona needs
    RocketRide: use(pipe) -> send(question carrying those rows) -> terminate
    Hotdata: load the reply back into the db -> drop the db
    Grading: deterministic rules + an LLM judge
    Telemetry: one row in loadtest_telemetry.public.persona_runs

Concurrency is held by a gate that starts at --concurrency and shrinks to
--rate-limit-floor the first time anything reports a rate limit; the affected
turn is then retried once at the lower width and the downgrade is reported.

Usage:
    python run_v1.py                       # N=8, concurrency 8, prompt_version v1
    python run_v1.py --n 2 --no-telemetry
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import anthropic
from dotenv import load_dotenv
from rocketride import RocketRideClient

import grader
import telemetry
import venue_db
from run_loadtest import WEBHOOK_SOURCE_ID, answer_texts, ensure_ca_bundle, require_env

ROOT = Path(__file__).resolve().parent
PIPELINES_DIR = ROOT / "pipelines" / "serial"
DEFAULT_URI = "https://api.rocketride.ai"
TELEMETRY_TABLE = "persona_runs"

TRIGGER_HEADER = (
    "A guest has just messaged the front desk. Reply to them.\n\n"
    "Retrieved from the venue database for this enquiry:\n\n"
)

RATE_LIMIT_MARKERS = ("rate limit", "rate_limit", "429", "too many requests",
                      "throttl", "quota exceeded")


def looks_rate_limited(exc: BaseException) -> bool:
    if isinstance(exc, anthropic.RateLimitError):
        return True
    text = f"{type(exc).__name__}: {exc}".lower()
    return any(m in text for m in RATE_LIMIT_MARKERS)


class Gate:
    """Concurrency gate that shrinks once, permanently, on a rate limit.

    Shrinking retires permits rather than recreating the semaphore, so turns
    already in flight are never interrupted -- the width just narrows as they
    finish.
    """

    def __init__(self, width: int, floor: int) -> None:
        self.width = width
        self.floor = min(floor, width)
        self._sem = asyncio.Semaphore(width)
        self._lock = asyncio.Lock()
        self.downgraded = False
        self.reason = ""

    async def __aenter__(self) -> "Gate":
        await self._sem.acquire()
        return self

    async def __aexit__(self, *exc: Any) -> None:
        self._sem.release()

    async def downgrade(self, reason: str) -> bool:
        """Retire permits down to the floor. Returns True if this call did it."""
        async with self._lock:
            if self.downgraded or self.width <= self.floor:
                return False
            self.downgraded = True
            self.reason = reason
            retire = self.width - self.floor
            self.width = self.floor
        for _ in range(retire):
            asyncio.create_task(self._retire())
        return True

    async def _retire(self) -> None:
        await self._sem.acquire()  # never released


@dataclass
class TurnResult:
    persona: dict[str, Any]
    ok: bool = False
    reply: str = ""
    question: str = ""
    retrieved: str = ""
    error: str = ""
    attempts: int = 1
    rate_limited: bool = False
    wall_s: float = 0.0
    pipeline_s: float = 0.0
    rules: grader.RuleResult | None = None
    llm_pass: bool | None = None
    llm_reason: str = ""
    life: venue_db.Lifecycle | None = None
    notes: list[str] = field(default_factory=list)

    @property
    def rule_pass(self) -> bool | None:
        return None if self.rules is None else self.rules.rule_pass

    @property
    def agree(self) -> bool | None:
        if self.rules is None or self.llm_pass is None:
            return None
        return self.rules.rule_pass == self.llm_pass


async def run_pipeline(uri: str, key: str, pipe: Path, question: str,
                       env: dict[str, str]) -> tuple[str, float]:
    """One single-branch pipeline: connect -> use -> send -> terminate."""
    started = time.perf_counter()
    client = RocketRideClient(uri=uri, auth=key)
    await client.connect()
    token: str | None = None
    try:
        result = await client.use(filepath=str(pipe), source=WEBHOOK_SOURCE_ID, ttl=0, env=env)
        token = result["token"]
        response = await client.send(
            token=token, data=question.encode("utf-8"),
            mimetype="text/plain", objinfo={"mimetype": "text/plain"},
        )
        answers = answer_texts(response)
        if not answers:
            raise RuntimeError(f"pipeline returned no answer: {response!r:.300}")
        return answers[0], time.perf_counter() - started
    finally:
        if token is not None:
            try:
                await client.terminate(token)
            except Exception:  # noqa: BLE001 - terminate failure must not mask the real error
                pass
        await client.disconnect()


async def run_turn(persona: dict[str, Any], ctx: dict[str, Any]) -> TurnResult:
    """One persona end to end. The database is always dropped."""
    out = TurnResult(persona=persona)
    pid = persona["id"]
    started = time.perf_counter()
    db = venue_db.PersonaDB(pid, ctx["run_id"])
    out.life = db.life

    try:
        await asyncio.to_thread(db.create)
        await asyncio.to_thread(db.load_reference)
        results = await asyncio.to_thread(db.query)
        out.retrieved = venue_db.format_retrieved(results)
        out.question = TRIGGER_HEADER + out.retrieved

        pipe = PIPELINES_DIR / f"loadtest-{pid}.pipe"
        if not pipe.exists():
            raise FileNotFoundError(f"{pipe} missing -- run: python gen_pipe.py --n 8 --serial")

        out.reply, out.pipeline_s = await run_pipeline(
            ctx["uri"], ctx["rr_key"], pipe, out.question, ctx["pipe_env"]
        )

        await asyncio.to_thread(db.load_reply, {
            "run_id": ctx["run_id"],
            "prompt_version": ctx["prompt_version"],
            "persona_id": pid,
            "persona_type": persona.get("type", ""),
            "ts": telemetry.utc_now(),
            "first_message": persona["first_message"],
            "question": out.question,
            "reply": out.reply,
            "reply_words": grader.word_count(out.reply),
        })

        out.rules = grader.apply_rules(out.reply, persona.get("checks") or {})
        if persona.get("llm_check"):
            out.llm_pass, out.llm_reason = await grader.llm_check(
                ctx["judge"], persona, out.reply, ctx["faq"], model=ctx["judge_model"]
            )
        out.ok = True
    except Exception as exc:  # noqa: BLE001 - one turn's failure must not end the run
        out.error = f"{type(exc).__name__}: {exc}"[:500]
        out.rate_limited = looks_rate_limited(exc)
    finally:
        if db.life.db_id:
            try:
                await asyncio.to_thread(db.drop)
            except Exception as exc:  # noqa: BLE001
                out.notes.append(f"db drop failed ({exc}); the 1h TTL will collect it")
        out.wall_s = time.perf_counter() - started

    return out


async def run_persona(persona: dict[str, Any], ctx: dict[str, Any], gate: Gate) -> TurnResult:
    """Run one persona under the gate, retrying once if it was rate limited."""
    async with gate:
        result = await run_turn(persona, ctx)

    if result.ok or not result.rate_limited:
        return result

    if await gate.downgrade(result.error):
        print(f"  [rate limit] {persona['id']}: {result.error[:120]}")
        print(f"  [rate limit] concurrency -> {gate.width}; retrying {persona['id']}")
    else:
        print(f"  [rate limit] {persona['id']}: retrying at concurrency {gate.width}")
    await asyncio.sleep(ctx["retry_delay"])
    async with gate:
        retry = await run_turn(persona, ctx)
    retry.attempts = result.attempts + 1
    retry.rate_limited = True
    retry.notes.append(f"first attempt rate limited: {result.error[:200]}")
    return retry


def print_summary(results: list[TurnResult], wall: float, gate: Gate) -> None:
    print("\n" + "=" * 78)
    print(f"{'persona':<6}{'type':<20}{'wall':>7}{'pipe':>7}{'rule':>7}{'llm':>7}{'agree':>7}  flags")
    for r in results:
        rule = "-" if r.rule_pass is None else ("PASS" if r.rule_pass else "FAIL")
        llm = "-" if r.llm_pass is None else ("PASS" if r.llm_pass else "FAIL")
        agree = "-" if r.agree is None else ("yes" if r.agree else "NO")
        flags = ",".join(r.rules.flags) if r.rules and r.rules.flags else ""
        if not r.ok:
            flags = f"ERROR {r.error[:40]}"
        print(f"{r.persona['id']:<6}{r.persona.get('type',''):<20}{r.wall_s:>6.1f}s"
              f"{r.pipeline_s:>6.1f}s{rule:>7}{llm:>7}{agree:>7}  {flags}")
    print("=" * 78)

    ok = [r for r in results if r.ok]
    rule_pass = sum(1 for r in ok if r.rule_pass)
    llm_pass = sum(1 for r in ok if r.llm_pass)
    agree = sum(1 for r in ok if r.agree)
    print(f"turns: {len(ok)}/{len(results)} completed   wall clock: {wall:.1f}s")
    print(f"rule_pass {rule_pass}/{len(ok)}   llm_pass {llm_pass}/{len(ok)}   "
          f"agree {agree}/{len(ok)}   (final verdict = llm_pass)")
    if gate.downgraded:
        print(f"concurrency was downgraded to {gate.width}: {gate.reason[:160]}")


def write_transcripts(path: Path, results: list[TurnResult], ctx: dict[str, Any],
                      wall: float, gate: Gate) -> None:
    lines: list[str] = [
        f"# Persona transcripts — prompt_version {ctx['prompt_version']}",
        "",
        f"- run id: `{ctx['run_id']}`",
        f"- started: {ctx['started']}",
        f"- RocketRide: `{ctx['uri']}` (architecture B — {len(results)} concurrent "
        f"single-branch pipelines, fan-in in Python)",
        f"- judge model: `{ctx['judge_model']}`",
        f"- concurrency: requested {ctx['concurrency']}"
        + (f", downgraded to {gate.width} after a rate limit" if gate.downgraded else ""),
        f"- wall clock: {wall:.1f}s",
        "",
        "Final verdict is `llm_pass`. `rule_pass` is the deterministic tripwire; "
        "`must_not_contain` hits are recorded as flags and never fail a turn.",
        "",
        "| persona | type | rule_pass | llm_pass | agree | flags |",
        "| --- | --- | --- | --- | --- | --- |",
    ]
    for r in results:
        rule = "—" if r.rule_pass is None else ("pass" if r.rule_pass else "**fail**")
        llm = "—" if r.llm_pass is None else ("pass" if r.llm_pass else "**fail**")
        agree = "—" if r.agree is None else ("yes" if r.agree else "**no**")
        flags = ", ".join(f"`{f}`" for f in r.rules.flags) if r.rules and r.rules.flags else "—"
        lines.append(f"| {r.persona['id']} | {r.persona.get('type','')} | {rule} | {llm} "
                     f"| {agree} | {flags} |")

    for r in results:
        p = r.persona
        lines += ["", "---", "", f"## {p['id']} — {p.get('type','')}", "",
                  "### first_message", "", "> " + p["first_message"].replace("\n", "\n> "), ""]
        if r.retrieved:
            lines += ["### retrieved from the persona's Hotdata database", "",
                      "```", r.retrieved, "```", ""]
        if not r.ok:
            lines += ["### outcome", "", f"**FAILED** — {r.error}", ""]
        else:
            lines += ["### reply", "", r.reply.strip(), "",
                      f"*{grader.word_count(r.reply)} words, "
                      f"pipeline {r.pipeline_s:.1f}s, turn {r.wall_s:.1f}s*", ""]
            lines += ["### rule result", ""]
            if r.rules is None or not r.rules.results:
                lines.append("_no positive rules for this persona._")
            else:
                lines += [f"- **{'PASS' if r.rules.rule_pass else 'FAIL'}** overall"]
                for c in r.rules.results:
                    lines.append(f"  - `{c['rule']}`: {'pass' if c['passed'] else '**fail**'}"
                                 f" — {c['detail']}")
            if r.rules and r.rules.flags:
                lines.append(f"- rule_flags (advisory, not a failure): "
                             + ", ".join(f"`{f}`" for f in r.rules.flags))
            if r.rules and r.rules.skipped:
                lines.append(f"- skipped (not in the grader schema): "
                             + ", ".join(f"`{k}`" for k in r.rules.skipped))
            # Only meaningful when a first_sentence rule actually ran; otherwise the
            # "sentence" is just however far the reply gets before a full stop.
            if r.rules and (p.get("checks") or {}).get("first_sentence_must_contain_any"):
                lines.append(f"- first sentence graded: _{r.rules.graded_first_sentence}_")
            lines += ["", "### llm result", ""]
            verdict = "—" if r.llm_pass is None else ("**PASS**" if r.llm_pass else "**FAIL**")
            lines += [f"- question: _{p.get('llm_check','')}_",
                      f"- verdict: {verdict}",
                      f"- reason: {r.llm_reason or '—'}"]
        if r.life:
            steps = "  ".join(f"{s.name} {s.seconds:.1f}s" for s in r.life.steps)
            lines += ["", "### database lifecycle", "",
                      f"- catalog `{r.life.catalog}`, id `{r.life.db_id or '—'}`, dropped",
                      f"- {r.life.queries_run} queries, {r.life.rows_retrieved} rows "
                      f"({r.life.empty_results} empty results)",
                      f"- {steps}"]
        for note in r.notes:
            lines.append(f"- note: {note}")

    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


def telemetry_rows(results: list[TurnResult], ctx: dict[str, Any], gate: Gate) -> list[dict[str, Any]]:
    rows = []
    for r in results:
        life = r.life
        rows.append({
            "ts": telemetry.utc_now(),
            "run_id": ctx["run_id"],
            "prompt_version": ctx["prompt_version"],
            "mode": "B",
            "n": ctx["n"],
            "concurrency_requested": ctx["concurrency"],
            "concurrency_effective": gate.width,
            "persona_id": r.persona["id"],
            "persona_type": r.persona.get("type", ""),
            "ok": r.ok,
            "error": r.error,
            "attempts": r.attempts,
            "rate_limited": r.rate_limited,
            "wall_s": round(r.wall_s, 3),
            "pipeline_s": round(r.pipeline_s, 3),
            "reply_words": grader.word_count(r.reply) if r.reply else 0,
            "rule_pass": r.rule_pass,
            "llm_pass": r.llm_pass,
            "agree": r.agree,
            "rule_flags": ",".join(r.rules.flags) if r.rules else "",
            "rule_skipped": ",".join(r.rules.skipped) if r.rules else "",
            "llm_reason": r.llm_reason[:300],
            "db_id": life.db_id if life else "",
            "db_catalog": life.catalog if life else "",
            "db_create_s": life.seconds("db_create") if life else 0.0,
            "db_load_s": life.seconds("db_load") if life else 0.0,
            "db_query_s": life.seconds("db_query") if life else 0.0,
            "reply_load_s": life.seconds("reply_load") if life else 0.0,
            "db_drop_s": life.seconds("db_drop") if life else 0.0,
            "db_total_s": life.total_seconds if life else 0.0,
            "rows_retrieved": life.rows_retrieved if life else 0,
            "queries_run": life.queries_run if life else 0,
            "empty_results": life.empty_results if life else 0,
            "db_step_failures": ",".join(s.name for s in life.failures) if life else "",
            "uri": ctx["uri"],
            "judge_model": ctx["judge_model"],
        })
    return rows


async def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--n", type=int, default=8)
    ap.add_argument("--concurrency", type=int, default=8)
    ap.add_argument("--rate-limit-floor", type=int, default=4)
    ap.add_argument("--prompt-version", default="v1")
    ap.add_argument("--judge-model", default=grader.JUDGE_MODEL)
    ap.add_argument("--retry-delay", type=float, default=5.0)
    ap.add_argument("--out", type=Path, default=None)
    ap.add_argument("--no-telemetry", action="store_true")
    args = ap.parse_args()

    load_dotenv(ROOT / ".env")
    ensure_ca_bundle()

    personas = json.loads((ROOT / "personas.json").read_text(encoding="utf-8"))[: args.n]
    faq = (ROOT / "faq.md").read_text(encoding="utf-8")

    ctx: dict[str, Any] = {
        "run_id": uuid.uuid4().hex[:8],
        "started": telemetry.utc_now(),
        "uri": os.environ.get("ROCKETRIDE_URI", DEFAULT_URI),
        "rr_key": require_env("ROCKETRIDE_API_KEY"),
        "pipe_env": {"ROCKETRIDE_ANTHROPIC_KEY": require_env("ANTHROPIC_API_KEY")},
        "faq": faq,
        "prompt_version": args.prompt_version,
        "judge_model": args.judge_model,
        "judge": anthropic.AsyncAnthropic(api_key=require_env("ANTHROPIC_API_KEY")),
        "retry_delay": args.retry_delay,
        "n": len(personas),
        "concurrency": args.concurrency,
    }
    out_path = args.out or ROOT / f"transcripts_{args.prompt_version}.md"

    print(f"run {ctx['run_id']}: {len(personas)} personas, architecture B, "
          f"concurrency {args.concurrency} (floor {args.rate_limit_floor})")
    print(f"  RocketRide {ctx['uri']}   judge {ctx['judge_model']}\n")

    gate = Gate(args.concurrency, args.rate_limit_floor)
    started = time.perf_counter()
    results = await asyncio.gather(*(run_persona(p, ctx, gate) for p in personas))
    wall = time.perf_counter() - started

    print_summary(list(results), wall, gate)
    write_transcripts(out_path, list(results), ctx, wall, gate)
    try:
        shown = out_path.relative_to(ROOT)
    except ValueError:
        shown = out_path
    print(f"\nwrote {shown}")

    if not args.no_telemetry:
        telemetry.record(telemetry_rows(list(results), ctx, gate), table=TELEMETRY_TABLE)

    return 0 if all(r.ok for r in results) else 1


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
