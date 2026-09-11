"""Append benchmark rows to the Hotdata telemetry database.

Instant databases reject INSERT/DDL over the query API (`COPY TO, DML, and DDL
statements are not supported`), so rows are written the supported way: staged to
a local JSON file and loaded with

    hotdata databases load --catalog <catalog> --table <table> --file <f> --append

Catalog and table come from the env so the target can move without code edits.
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


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def record(rows: list[dict[str, Any]], *, catalog: str = CATALOG, table: str = TABLE) -> bool:
    """Append rows to the telemetry table. Returns True on success.

    Never raises: a telemetry failure must not lose the measurement that was
    just taken, so the caller can still print it.
    """
    if not rows:
        return True
    tmp = Path(tempfile.mkdtemp(prefix="rr-telemetry-")) / "rows.json"
    tmp.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    cmd = [
        "hotdata", "databases", "load",
        "--catalog", catalog, "--table", table,
        "--file", str(tmp), "--format", "json", "--append", "--no-input",
    ]
    proc = subprocess.run(cmd, capture_output=True, text=True)
    if proc.returncode != 0:
        print(f"[telemetry] load failed (rc={proc.returncode}): "
              f"{(proc.stderr or proc.stdout).strip()[:400]}")
        return False
    print(f"[telemetry] wrote {len(rows)} row(s) to {catalog}.{table}")
    return True
