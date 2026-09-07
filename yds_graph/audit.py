"""Audit rows. Every run writes one, including the runs that stop early.

A pipeline that only records its successes is a pipeline nobody can trust.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from . import config


SCHEMA = """
CREATE TABLE IF NOT EXISTS audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    run_at TEXT NOT NULL,
    thread_id TEXT NOT NULL,
    kind TEXT NOT NULL,
    coach TEXT,
    source_file TEXT,
    source_sha256 TEXT,
    rows INTEGER,
    date_range TEXT,
    pipeline_version TEXT NOT NULL,
    facts_schema TEXT,
    rules_version TEXT,
    outcome TEXT NOT NULL,
    reason TEXT,
    artifact TEXT,
    detail_json TEXT
);
"""


def connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path else config.audit_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    return conn


def write_row(
    *,
    thread_id: str,
    kind: str,
    outcome: str,
    coach: str | None = None,
    provenance: dict | None = None,
    reason: str | None = None,
    artifact: str | None = None,
    detail: dict | None = None,
    path: Path | None = None,
) -> dict:
    provenance = provenance or {}
    row = {
        "run_at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "thread_id": thread_id,
        "kind": kind,
        "coach": coach,
        "source_file": provenance.get("source_file"),
        "source_sha256": provenance.get("sha256"),
        "rows": provenance.get("rows"),
        "date_range": " to ".join(provenance.get("date_range", [])) or None,
        "pipeline_version": config.PIPELINE_VERSION,
        "facts_schema": config.FACTS_SCHEMA_VERSION,
        "rules_version": config.RULES_VERSION,
        "outcome": outcome,
        "reason": reason,
        "artifact": artifact,
        "detail_json": json.dumps(detail or {}, default=str),
    }
    conn = connect(path)
    with conn:
        conn.execute(
            "INSERT INTO audit ("
            + ", ".join(row)
            + ") VALUES ("
            + ", ".join(f":{k}" for k in row)
            + ")",
            row,
        )
    conn.close()
    return row


def read_rows(limit: int = 50, path: Path | None = None) -> list[sqlite3.Row]:
    conn = connect(path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT * FROM audit ORDER BY id DESC LIMIT ?", (limit,)
    ).fetchall()
    conn.close()
    return list(rows)


def format_table(rows: list[sqlite3.Row]) -> str:
    if not rows:
        return "No audit rows yet."
    header = f"{'id':>4}  {'run_at':<26} {'kind':<9} {'outcome':<9} {'coach':<10} {'source':<26} {'version':<16} reason"
    lines = [header, "-" * len(header)]
    for r in reversed(rows):
        lines.append(
            f"{r['id']:>4}  {r['run_at']:<26} {r['kind']:<9} {r['outcome']:<9} "
            f"{(r['coach'] or ''):<10} {(r['source_file'] or ''):<26} "
            f"{r['pipeline_version']:<16} {(r['reason'] or '')[:60]}"
        )
    return "\n".join(lines)
