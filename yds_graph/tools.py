"""The staff assistant's tools, and the SQLite database behind them.

Retrieval here is SQL. There is no vector store, because the questions a
staff actually asks ("who else has played shortstop", "when do we travel")
are queries, not similarity searches, and a query either answers or says it
found nothing.
"""

from __future__ import annotations

import json
import re
import sqlite3
from pathlib import Path

import pandas as pd

from . import config
from .adapters import adapt
from .facts import compute_facts, file_provenance


SCHEMA = """
CREATE TABLE IF NOT EXISTS roster (
    player TEXT PRIMARY KEY,
    positions_played TEXT,
    bats TEXT,
    throws TEXT,
    notes TEXT
);
CREATE TABLE IF NOT EXISTS schedule (
    date TEXT,
    opponent TEXT,
    site TEXT,
    travel TEXT
);
CREATE TABLE IF NOT EXISTS opponent_facts (
    pitcher TEXT,
    fact_id TEXT,
    metric TEXT,
    label TEXT,
    value REAL,
    n INTEGER,
    unit TEXT,
    version TEXT,
    source_file TEXT
);
CREATE TABLE IF NOT EXISTS assistant_notes (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT,
    session_id TEXT,
    question TEXT,
    answer TEXT
);
"""

SELECT_ONLY = re.compile(r"^\s*select\b", re.IGNORECASE)
FORBIDDEN = re.compile(
    r"\b(insert|update|delete|drop|alter|create|attach|pragma|replace|vacuum)\b", re.IGNORECASE
)


class ToolError(RuntimeError):
    pass


def build_db(path: Path | None = None, sample_dir: Path | None = None) -> Path:
    db_path = Path(path) if path else config.assistant_db_path()
    sample = Path(sample_dir) if sample_dir else config.sample_data_dir()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path)
    conn.executescript(SCHEMA)
    with conn:
        conn.execute("DELETE FROM roster")
        conn.execute("DELETE FROM schedule")
        conn.execute("DELETE FROM opponent_facts")
        pd.read_csv(sample / "roster.csv").to_sql("roster", conn, if_exists="append", index=False)
        pd.read_csv(sample / "schedule.csv").to_sql("schedule", conn, if_exists="append", index=False)

        export = sample / "pitch_export_a.csv"
        frame, _ = adapt(pd.read_csv(export))
        sheet = compute_facts(frame, file_provenance(export, frame))
        rows = [
            (
                fact["pitcher"],
                fact["id"],
                fact["metric"],
                fact["label"],
                fact["value"],
                fact["n"],
                fact["unit"],
                sheet["version"],
                sheet["provenance"]["source_file"],
            )
            for fact in sheet["facts"].values()
        ]
        conn.executemany(
            "INSERT INTO opponent_facts VALUES (?,?,?,?,?,?,?,?,?)", rows
        )
    conn.close()
    return db_path


def _connect(path: Path | None = None) -> sqlite3.Connection:
    db_path = Path(path) if path else config.assistant_db_path()
    if not db_path.exists():
        build_db(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _run_select(sql: str, table: str, path: Path | None = None) -> str:
    if not SELECT_ONLY.match(sql) or FORBIDDEN.search(sql) or ";" in sql.strip().rstrip(";"):
        raise ToolError("Only a single SELECT statement is allowed.")
    if table not in sql.lower():
        raise ToolError(f"This tool only reads the {table} table.")
    conn = _connect(path)
    try:
        rows = conn.execute(sql).fetchall()
    except sqlite3.Error as exc:
        raise ToolError(f"SQL error: {exc}") from exc
    finally:
        conn.close()
    if not rows:
        return "No rows matched."
    return json.dumps([dict(r) for r in rows], indent=None)


def query_roster(sql: str, path: Path | None = None) -> str:
    """Read-only SELECT against the roster table."""
    return _run_select(sql, "roster", path)


def query_schedule(sql: str, path: Path | None = None) -> str:
    """Read-only SELECT against the schedule table."""
    return _run_select(sql, "schedule", path)


def opponent_facts(pitcher: str, path: Path | None = None) -> str:
    """Every computed fact for one opponent pitcher, with fact ids and n."""
    conn = _connect(path)
    rows = conn.execute(
        "SELECT fact_id, label, value, n, unit, version, source_file "
        "FROM opponent_facts WHERE pitcher = ? ORDER BY rowid",
        (pitcher,),
    ).fetchall()
    conn.close()
    if not rows:
        available = list_pitchers(path)
        return f"No facts on file for {pitcher}. On file: {', '.join(available) or 'none'}."
    header = (
        f"Facts for {pitcher} | {rows[0]['version']} | source {rows[0]['source_file']}"
    )
    lines = [header]
    for r in rows:
        unit = "%" if r["unit"] == "percent" else f" {r['unit']}"
        lines.append(f"[{r['fact_id']}] {r['label']}: {r['value']}{unit} (n={r['n']})")
    return "\n".join(lines)


def list_pitchers(path: Path | None = None) -> list[str]:
    conn = _connect(path)
    rows = conn.execute("SELECT DISTINCT pitcher FROM opponent_facts ORDER BY pitcher").fetchall()
    conn.close()
    return [r["pitcher"] for r in rows]


MESSAGE_KINDS = {"parents", "players", "staff"}


def draft_message(kind: str, points: list[str] | str) -> str:
    """Assemble a plain draft from the staff's own messaging rules.

    Deliberately deterministic. The draft is assembled from the points it was
    given, so nothing is invented at this step, and every draft for parents,
    players or staff still has to clear a human before it goes anywhere.
    """
    if isinstance(points, str):
        points = [p.strip() for p in points.split("\n") if p.strip()]
    kind = (kind or "").strip().lower()

    philosophy = config.sample_data_dir() / "philosophy.md"
    rules = philosophy.read_text() if philosophy.exists() else ""

    openers = {
        "parents": "Quick note from the staff.",
        "players": "Team, one thing before the weekend.",
        "staff": "Staff, here is where we are.",
    }
    closers = {
        "parents": "If you have a question about your own son, call the coach directly.",
        "players": "Bring it Friday.",
        "staff": "Push back on any of this before we send it.",
    }
    opener = openers.get(kind, "Note.")
    closer = closers.get(kind, "")

    body = "\n".join(f"- {p}" for p in points)
    draft = f"{opener}\n\n{body}\n\n{closer}".strip()

    status = (
        "NEEDS HUMAN REVIEW. Not sent."
        if kind in MESSAGE_KINDS
        else "Internal note. Still not sent by this tool."
    )
    guidance = "Messaging rules applied:\n" + rules if rules else ""
    return f"kind={kind}\nstatus={status}\n\n--- draft ---\n{draft}\n--- end draft ---\n\n{guidance}"


TOOL_SPECS = [
    {
        "name": "query_roster",
        "description": (
            "Run one read-only SELECT against the roster table. Columns: player, "
            "positions_played, bats, throws, notes. positions_played is a slash "
            "separated string such as 'SS/2B/3B'."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
    {
        "name": "query_schedule",
        "description": (
            "Run one read-only SELECT against the schedule table. Columns: date "
            "(YYYY-MM-DD), opponent, site, travel."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"sql": {"type": "string"}},
            "required": ["sql"],
            "additionalProperties": False,
        },
    },
    {
        "name": "opponent_facts",
        "description": (
            "Every computed fact for one opponent pitcher, by pitcher id. These are "
            "the only numbers you may state about an opponent pitcher."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {"pitcher": {"type": "string"}},
            "required": ["pitcher"],
            "additionalProperties": False,
        },
    },
    {
        "name": "draft_message",
        "description": (
            "Assemble a draft message from bullet points, using the staff's own "
            "messaging rules. kind is one of parents, players, staff, or internal. "
            "Nothing this tool produces is sent."
        ),
        "strict": True,
        "input_schema": {
            "type": "object",
            "properties": {
                "kind": {"type": "string", "enum": ["parents", "players", "staff", "internal"]},
                "points": {"type": "array", "items": {"type": "string"}},
            },
            "required": ["kind", "points"],
            "additionalProperties": False,
        },
    },
]


def execute(name: str, arguments: dict, path: Path | None = None) -> str:
    try:
        if name == "query_roster":
            return query_roster(arguments["sql"], path)
        if name == "query_schedule":
            return query_schedule(arguments["sql"], path)
        if name == "opponent_facts":
            return opponent_facts(arguments["pitcher"], path)
        if name == "draft_message":
            return draft_message(arguments["kind"], arguments.get("points", []))
    except ToolError as exc:
        return f"TOOL ERROR: {exc}"
    return f"TOOL ERROR: unknown tool {name}"
