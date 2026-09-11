"""Per-persona Hotdata database: the stand-in for agent tool use.

NOTES.md fact 9: an `agent_deepagent` on RocketRide Cloud discovers its tools
but never executes them, so the agent cannot query a database itself. This
module moves that step to Python and keeps the database in the loop:

    create -> load reference tables -> query the rows this persona needs ->
    (caller puts the rows in the question) -> load the reply back -> drop

Each persona gets its OWN database, created with a 1h TTL (NOTES.md fact 7) and
dropped at the end of the turn, so a crashed run self-cleans. Every step is
timed; the caller records the timings as telemetry.

The catalog alias must be globally unique across the workspace, so it carries a
per-run suffix.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

# --- Reference data, transcribed from faq.md -------------------------------
# Kept as rows rather than prose so the retrieval step is a real SQL query.

PRICING_ROWS: list[dict[str, Any]] = [
    {"item": "weekday_hourly", "amount": 180, "unit": "per hour",
     "minimum": "4-hour minimum", "notes": "Mon-Thu"},
    {"item": "weekend_hourly", "amount": 260, "unit": "per hour",
     "minimum": "5-hour minimum", "notes": "Fri-Sun"},
    {"item": "buyout_weekday", "amount": 2400, "unit": "full-day buyout",
     "minimum": "", "notes": "Mon-Thu"},
    {"item": "buyout_weekend", "amount": 3600, "unit": "full-day buyout",
     "minimum": "", "notes": "Fri-Sun"},
    {"item": "security_deposit", "amount": 500, "unit": "per event",
     "minimum": "", "notes": "returned within 7 days after event"},
    {"item": "catering_in_house_min", "amount": 35, "unit": "per person",
     "minimum": "", "notes": "in-house catering, low end of range"},
    {"item": "catering_in_house_max", "amount": 65, "unit": "per person",
     "minimum": "", "notes": "in-house catering, high end of range"},
    {"item": "outside_caterer_fee", "amount": 200, "unit": "per event",
     "minimum": "", "notes": "kitchen fee when an outside caterer is used"},
]

AVAILABILITY_ROWS: list[dict[str, Any]] = [
    {"date": "10/3", "day": "Sat", "status": "open", "notes": "October open date"},
    {"date": "10/10", "day": "Sat", "status": "open", "notes": "October open date"},
    {"date": "10/17", "day": "Sat", "status": "open", "notes": "October open date"},
    {"date": "10/weekdays", "day": "weekday", "status": "open",
     "notes": "most October weekdays are open"},
]

VENUE_FACTS_ROWS: list[dict[str, Any]] = [
    {"topic": "location", "detail": "1200 Market St, San Francisco, 2nd floor, elevator access"},
    {"topic": "capacity", "detail": "40 seated / 80 standing"},
    {"topic": "hours", "detail": "Mon-Thu 9am-10pm, Fri-Sun 9am-midnight"},
    {"topic": "av", "detail": "projector, 2 wireless mics, Sonos system included"},
    {"topic": "parking", "detail": "30-space lot behind building, free for guests"},
    {"topic": "decorations", "detail": "no open flame, no confetti, no tape on walls"},
    {"topic": "booking", "detail": "50% deposit to hold a date; balance due 14 days before event"},
    {"topic": "booking", "detail": "setup and teardown: 1 hour before and after, included free"},
    {"topic": "booking", "detail": "security deposit $500, returned within 7 days after event"},
]

TABLES: dict[str, list[dict[str, Any]]] = {
    "pricing": PRICING_ROWS,
    "availability": AVAILABILITY_ROWS,
    "venue_facts": VENUE_FACTS_ROWS,
}
# Written to, not read: the turn's reply is loaded back into the persona's own
# database before it is dropped.
REPLY_TABLE = "replies"

# --- What each persona's turn retrieves ------------------------------------
# `{c}` is the persona's catalog. A query that legitimately returns NOTHING is
# as much a part of the test as one that returns rows: p5 asks about a date that
# is not on the books and p6 asks about BYOB, which the venue has no policy for.
# An empty result is the signal the agent is supposed to act on.

PERSONA_QUERIES: dict[str, list[tuple[str, str]]] = {
    "p1": [
        ("pricing", "SELECT item, amount, unit, minimum, notes FROM {c}.public.pricing "
                    "WHERE item IN ('weekend_hourly', 'buyout_weekend', 'buyout_weekday', "
                    "'security_deposit') ORDER BY item"),
        ("availability for 10/10", "SELECT date, day, status FROM {c}.public.availability "
                                   "WHERE date = '10/10'"),
    ],
    "p2": [
        ("venue facts", "SELECT topic, detail FROM {c}.public.venue_facts "
                        "WHERE topic IN ('capacity', 'av')"),
        ("pricing", "SELECT item, amount, unit, minimum FROM {c}.public.pricing "
                    "WHERE item IN ('weekday_hourly', 'catering_in_house_min', "
                    "'catering_in_house_max', 'outside_caterer_fee') ORDER BY item"),
    ],
    "p3": [
        ("deposit terms", "SELECT item, amount, unit, notes FROM {c}.public.pricing "
                          "WHERE item = 'security_deposit'"),
        ("availability for 10/3 and 10/17", "SELECT date, day, status FROM {c}.public.availability "
                                            "WHERE date IN ('10/3', '10/17') ORDER BY date"),
        ("booking terms", "SELECT detail FROM {c}.public.venue_facts WHERE topic = 'booking'"),
    ],
    "p4": [
        ("booking terms", "SELECT detail FROM {c}.public.venue_facts "
                          "WHERE topic IN ('booking', 'location')"),
        ("deposit terms", "SELECT item, amount, unit, notes FROM {c}.public.pricing "
                          "WHERE item = 'security_deposit'"),
    ],
    "p5": [
        # Sept 19 is not on the books -- expected to return zero rows.
        ("availability for 9/19", "SELECT date, day, status FROM {c}.public.availability "
                                  "WHERE date = '9/19'"),
    ],
    "p6": [
        # BYOB / corkage is not a thing this venue has published -- zero rows.
        ("alcohol or corkage policy", "SELECT item, amount, unit, notes FROM {c}.public.pricing "
                                      "WHERE item LIKE '%corkage%' OR item LIKE '%alcohol%' "
                                      "OR item LIKE '%byob%' OR item LIKE '%wine%'"),
        ("catering pricing", "SELECT item, amount, unit FROM {c}.public.pricing "
                             "WHERE item LIKE '%cater%' ORDER BY item"),
    ],
    "p7": [
        ("all pricing", "SELECT item, amount, unit, minimum FROM {c}.public.pricing ORDER BY item"),
        ("all availability", "SELECT date, day, status FROM {c}.public.availability ORDER BY date"),
    ],
    "p8": [
        ("parking", "SELECT topic, detail FROM {c}.public.venue_facts WHERE topic = 'parking'"),
    ],
}


@dataclass
class Step:
    name: str
    seconds: float
    ok: bool
    detail: str = ""


@dataclass
class Lifecycle:
    """Everything that happened to one persona's database, for the telemetry row."""
    persona_id: str
    catalog: str
    db_id: str = ""
    steps: list[Step] = field(default_factory=list)
    rows_retrieved: int = 0
    queries_run: int = 0
    empty_results: int = 0

    def add(self, name: str, seconds: float, ok: bool, detail: str = "") -> None:
        self.steps.append(Step(name, seconds, ok, detail))

    def seconds(self, name: str) -> float:
        return round(sum(s.seconds for s in self.steps if s.name == name), 3)

    @property
    def total_seconds(self) -> float:
        return round(sum(s.seconds for s in self.steps), 3)

    @property
    def failures(self) -> list[Step]:
        return [s for s in self.steps if not s.ok]


class HotdataError(RuntimeError):
    pass


def _hotdata(*args: str, timeout: float = 180.0) -> dict[str, Any] | str:
    proc = subprocess.run(["hotdata", *args], capture_output=True, text=True, timeout=timeout)
    if proc.returncode != 0:
        raise HotdataError((proc.stderr or proc.stdout).strip()[:500] or f"rc={proc.returncode}")
    out = proc.stdout.strip()
    if not out:
        return ""
    try:
        return json.loads(out)
    except ValueError:
        return out


class PersonaDB:
    """One persona's throwaway database. Blocking -- call from asyncio.to_thread."""

    def __init__(self, persona_id: str, run_id: str, ttl: str = "1h") -> None:
        self.persona_id = persona_id
        self.run_id = run_id
        self.ttl = ttl
        # [a-z_][a-z0-9_]* and globally unique across the workspace.
        self.catalog = f"loadtest_{persona_id}_{run_id}".lower()
        self.life = Lifecycle(persona_id=persona_id, catalog=self.catalog)

    def _timed(self, name: str, fn, *a, **kw):
        started = time.perf_counter()
        try:
            result = fn(*a, **kw)
        except Exception as exc:  # noqa: BLE001 - recorded, then re-raised by the caller's choice
            self.life.add(name, time.perf_counter() - started, False, str(exc)[:300])
            raise
        self.life.add(name, time.perf_counter() - started, True)
        return result

    # -- lifecycle ---------------------------------------------------------

    def create(self) -> str:
        def _do() -> str:
            cmd = ["databases", "create", "--name", f"loadtest {self.persona_id} {self.run_id}",
                   "--catalog", self.catalog, "--expires-at", self.ttl, "-o", "json", "--no-input"]
            for table in (*TABLES, REPLY_TABLE):
                cmd += ["--table", table]
            payload = _hotdata(*cmd)
            db_id = payload.get("id", "") if isinstance(payload, dict) else ""
            if not db_id:
                raise HotdataError(f"create returned no id: {payload!r:.200}")
            return db_id

        self.life.db_id = self._timed("db_create", _do)
        return self.life.db_id

    def load_reference(self) -> None:
        def _do() -> None:
            for table, rows in TABLES.items():
                self._load(table, rows, append=False)
        self._timed("db_load", _do)

    def query(self) -> list[tuple[str, list[str], list[list[Any]]]]:
        """Run this persona's retrieval queries. Empty results are kept, not dropped."""
        def _do() -> list[tuple[str, list[str], list[list[Any]]]]:
            out = []
            for label, sql in PERSONA_QUERIES.get(self.persona_id, []):
                payload = _hotdata("databases", "query", "-d", self.life.db_id,
                                   sql.format(c=self.catalog), "-o", "json", "--no-input")
                if not isinstance(payload, dict):
                    raise HotdataError(f"query returned no JSON: {payload!r:.200}")
                columns = payload.get("columns") or []
                rows = payload.get("rows") or []
                out.append((label, columns, rows))
                self.life.queries_run += 1
                self.life.rows_retrieved += len(rows)
                if not rows:
                    self.life.empty_results += 1
            return out
        return self._timed("db_query", _do)

    def load_reply(self, row: dict[str, Any]) -> None:
        self._timed("reply_load", self._load, REPLY_TABLE, [row], append=True)

    def drop(self) -> None:
        self._timed("db_drop", _hotdata, "databases", "remove", self.life.db_id, "--no-input")

    # -- helpers -----------------------------------------------------------

    def _load(self, table: str, rows: list[dict[str, Any]], *, append: bool) -> None:
        """Instant databases reject INSERT/DDL, so rows go in via a staged file."""
        tmp = Path(tempfile.mkdtemp(prefix=f"venue-{self.persona_id}-")) / f"{table}.json"
        tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
        cmd = ["databases", "load", "--catalog", self.catalog, "--table", table,
               "--file", str(tmp), "--format", "json", "--no-input"]
        if append:
            cmd.append("--append")
        _hotdata(*cmd)


def format_retrieved(results: list[tuple[str, list[str], list[list[Any]]]]) -> str:
    """Render query results as the text block that goes into the question.

    An empty result is rendered explicitly rather than omitted -- "no rows" is
    the fact the agent has to notice for p5 (unlisted date) and p6 (no BYOB
    policy on file).
    """
    blocks = []
    for label, columns, rows in results:
        if not rows:
            blocks.append(f"[{label}]\n(no rows — nothing on file for this)")
            continue
        header = " | ".join(columns)
        body = "\n".join(
            " | ".join("" if v is None else str(v) for v in row) for row in rows
        )
        blocks.append(f"[{label}]\n{header}\n{body}")
    return "\n\n".join(blocks)
