"""Append benchmark rows to the Hotdata telemetry database.

Instant databases reject INSERT/DDL over the query API (`COPY TO, DML, and DDL
statements are not supported`), so rows are written the supported way: staged to
a local JSON file and loaded with

    hotdata databases load --catalog <catalog> --table <table> --file <f> --append

`load` targets a database by its (globally unique) catalog alias -- it has no
`--database` flag -- so the catalog is what routes the write. The database *id*
is tracked separately, in HOTDATA_TELEMETRY_DB_ID, because that is what
`databases show` / `create` speak.

Catalog, table and database id all come from the env so the target can move
without code edits.
"""

from __future__ import annotations

import json
import os
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

CATALOG = os.environ.get("HOTDATA_TELEMETRY_CATALOG", "loadtest_telemetry")
TABLE = os.environ.get("HOTDATA_TELEMETRY_TABLE", "runs")

DB_ID_VAR = "HOTDATA_TELEMETRY_DB_ID"
DB_NAME = "loadtest telemetry"
# Every table the project writes, declared up front so a fresh database has the
# same shape as the one we normally reuse.
DB_TABLES = ("runs", "findings")
# NOTES.md fact 7: scratch databases always get a TTL. Telemetry keeps 3d.
DB_EXPIRES_AT = "3d"


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _hotdata(*args: str) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["hotdata", *args], capture_output=True, text=True)


def _exists(db_id: str) -> bool:
    return _hotdata("databases", "show", db_id, "-o", "json", "--no-input").returncode == 0


def ensure_database(*, catalog: str = CATALOG) -> str | None:
    """Return the telemetry database id, creating a database only if we have none.

    Reuse is the point: the database named by HOTDATA_TELEMETRY_DB_ID holds the
    accumulated A/B timings, and a benchmark run is only comparable against the
    rows already in it. So:

      - id set and the database is there -> reuse it, create nothing.
      - id set but the database is gone (expired, deleted) -> refuse. Creating a
        silent empty replacement would look like success while losing the
        history the id was pointing at; that call is the operator's to make.
      - no id at all -> create one, and print the line to add to .env so the
        next run reuses it instead of making another.

    Returns None when no usable database could be established.
    """
    db_id = os.environ.get(DB_ID_VAR, "").strip()
    if db_id:
        if _exists(db_id):
            print(f"[telemetry] reusing database {db_id} (catalog {catalog})")
            return db_id
        print(f"[telemetry] {DB_ID_VAR}={db_id} names no database in this workspace. "
              f"Not creating a replacement -- that would silently drop the run "
              f"history. Clear {DB_ID_VAR} in .env to create a fresh one.")
        return None

    print(f"[telemetry] no {DB_ID_VAR} set; creating database {catalog!r} ...")
    cmd = ["databases", "create", "--name", DB_NAME, "--catalog", catalog,
           "--expires-at", DB_EXPIRES_AT, "-o", "json", "--no-input"]
    for table in DB_TABLES:
        cmd += ["--table", table]
    proc = _hotdata(*cmd)
    if proc.returncode != 0:
        print(f"[telemetry] create failed (rc={proc.returncode}): "
              f"{(proc.stderr or proc.stdout).strip()[:400]}")
        return None
    try:
        db_id = (json.loads(proc.stdout) or {}).get("id", "")
    except (ValueError, TypeError):
        db_id = ""
    if not db_id:
        print(f"[telemetry] create returned no database id: {proc.stdout.strip()[:400]}")
        return None
    os.environ[DB_ID_VAR] = db_id
    print(f"[telemetry] created database {db_id}. Add this line to .env so it is "
          f"reused:\n  {DB_ID_VAR}={db_id}")
    return db_id


def record(rows: list[dict[str, Any]], *, catalog: str = CATALOG, table: str = TABLE) -> bool:
    """Append rows to the telemetry table. Returns True on success.

    Never raises: a telemetry failure must not lose the measurement that was
    just taken, so the caller can still print it.
    """
    if not rows:
        return True
    if ensure_database(catalog=catalog) is None:
        print("[telemetry] no telemetry database; rows not written")
        return False
    tmp = Path(tempfile.mkdtemp(prefix="rr-telemetry-")) / "rows.json"
    tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    proc = _hotdata(
        "databases", "load",
        "--catalog", catalog, "--table", table,
        "--file", str(tmp), "--format", "json", "--append", "--no-input",
    )
    if proc.returncode != 0:
        print(f"[telemetry] load failed (rc={proc.returncode}): "
              f"{(proc.stderr or proc.stdout).strip()[:400]}")
        return False
    print(f"[telemetry] wrote {len(rows)} row(s) to {catalog}.{table}")
    return True
