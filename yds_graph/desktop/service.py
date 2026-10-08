"""The review actions behind the desktop page.

run, list the paused threads, approve, reject, and read the audit table.
The graph, the interrupt, and the audit row are the same code the CLI uses.
This module does not compute a number. It starts a run and reads what the
graph already stored.
"""

from __future__ import annotations

import base64
import os
import re
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any

from .. import assistant_graph, audit, config, library_graph, report_graph
from .errors import ServiceError
from .paths import prepare_desktop_home


_lock = threading.Lock()
_savers: dict[str, Any] = {}

_LABEL = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_-]{0,39}$")
_UPLOAD_LIMIT = 20 * 1024 * 1024


def _saver():
    path = config.checkpoint_db_path()
    key = str(path)
    saver = _savers.get(key)
    if saver is None:
        saver = report_graph.open_checkpointer(path)
        _savers[key] = saver
    return saver


def _safe_label(coach: str) -> str:
    label = (coach or "").strip() or "sample"
    if not _LABEL.match(label):
        raise ServiceError("Use letters and numbers for the folder label.")
    return label


def _interrupt_value(pending) -> dict | None:
    """Pull the paused payload off the newest checkpoint's pending writes.

    `list` yields newest checkpoints first. The first row for a thread is the
    one that matters. A paused review stores the interrupt on the
    `__interrupt__` channel.
    """
    if not pending:
        return None
    for item in pending:
        if len(item) < 3:
            continue
        channel, value = item[1], item[2]
        if channel != "__interrupt__" or not value:
            continue
        first = value[0] if isinstance(value, (list, tuple)) else value
        payload = getattr(first, "value", first)
        if isinstance(payload, dict):
            return payload
    return None


def _paused_because(kind: str) -> str:
    if kind == "report_review":
        return (
            "This report passed the checks and is waiting for a person. "
            "Nothing has been delivered."
        )
    if kind == "message_review":
        return (
            "A draft is waiting for a person to read it. "
            "This program does not send messages."
        )
    return "This run is waiting for a person."


def _public_review(thread_id: str, value: dict) -> dict:
    kind = str(value.get("kind") or "review")
    provenance = value.get("provenance") or {}
    return {
        "thread_id": thread_id,
        "kind": kind,
        "coach": value.get("coach"),
        "source_file": value.get("source_file"),
        "version": value.get("version"),
        "pitchers": list(value.get("pitchers") or []),
        "draft_notes": dict(value.get("draft_notes") or {}),
        "question": value.get("question"),
        "answer": value.get("answer"),
        "drafts": list(value.get("drafts") or []),
        "note": value.get("note"),
        "paused_because": _paused_because(kind),
        "provenance": {
            "rows": provenance.get("rows"),
            "date_range": provenance.get("date_range"),
            "games": provenance.get("games"),
        },
    }


def _present_result(thread_id: str, result: dict) -> dict:
    interrupts = result.get("__interrupt__") or []
    if interrupts:
        value = getattr(interrupts[0], "value", interrupts[0])
        if not isinstance(value, dict):
            value = {}
        review = _public_review(thread_id, value)
        return {"thread_id": thread_id, "status": "paused", "review": review}
    status = result.get("status") or "unknown"
    body: dict[str, Any] = {
        "thread_id": thread_id,
        "status": status,
        "outbox_path": result.get("outbox_path"),
        "hold_note_path": result.get("hold_note_path"),
        "answer": result.get("answer"),
        "persisted": result.get("persisted"),
    }
    failures = result.get("gate_failures") or []
    if failures:
        body["coach_notes"] = [item.get("coach_note") for item in failures if item.get("coach_note")]
    if result.get("hold_note_path"):
        note = Path(result["hold_note_path"])
        if note.is_file():
            body["hold_note"] = note.read_text(encoding="utf-8")
        body["message"] = "This file was held. Nothing was delivered."
    elif status == "delivered" and result.get("outbox_path"):
        body["message"] = "The PDF was saved."
    return body


def list_pending() -> list[dict]:
    """Threads sitting on a human interrupt. Finished runs are left out."""
    db = config.home() / "state" / "checkpoints.sqlite"
    if not db.is_file():
        return []
    with _lock:
        saver = _saver()
        try:
            rows = saver.list(None)
        except Exception as exc:
            raise ServiceError("The saved runs could not be read.") from exc
        seen: set[str] = set()
        pending: list[dict] = []
        for item in rows:
            cfg = item.config.get("configurable") or {}
            if cfg.get("checkpoint_ns"):
                continue
            thread_id = cfg.get("thread_id")
            if not thread_id or thread_id in seen:
                continue
            seen.add(thread_id)
            value = _interrupt_value(getattr(item, "pending_writes", None))
            if value is None:
                continue
            pending.append(_public_review(str(thread_id), value))
        return pending


def get_review(thread_id: str) -> dict:
    thread_id = (thread_id or "").strip()
    if not thread_id:
        raise ServiceError("Name the run to open.")
    for item in list_pending():
        if item["thread_id"] == thread_id:
            return item
    raise ServiceError("That run is not waiting for review.", status=404)


def start_report(csv_path: str | Path, coach: str = "sample") -> dict:
    path = Path(csv_path).expanduser()
    if not path.is_file():
        raise ServiceError(f"No file named {path.name} was found.")
    if path.suffix.lower() != ".csv":
        raise ServiceError("Choose a CSV file.")
    label = _safe_label(coach)
    thread_id = f"report-{uuid.uuid4().hex[:10]}"
    with _lock:
        result = report_graph.start_run(str(path.resolve()), label, thread_id, _saver())
    return _present_result(thread_id, result)


def start_sample(coach: str = "sample") -> dict:
    prepare_desktop_home(config.home())
    path = config.inbox_dir() / "sample.csv"
    if not path.is_file():
        raise ServiceError("The sample file is missing from this computer.")
    return start_report(path, coach)


def save_upload(filename: str, content_base64: str, coach: str = "sample") -> dict:
    name = Path(filename or "").name
    if not name.lower().endswith(".csv"):
        raise ServiceError("Choose a CSV file.")
    safe = re.sub(r"[^A-Za-z0-9._-]", "_", name).strip("._") or "upload.csv"
    if not safe.lower().endswith(".csv"):
        safe += ".csv"
    try:
        raw = base64.b64decode(content_base64 or "", validate=True)
    except Exception as exc:
        raise ServiceError("That file could not be read.") from exc
    if len(raw) > _UPLOAD_LIMIT:
        raise ServiceError("That file is larger than 20 MB.")
    if not raw.strip():
        raise ServiceError("That file is empty.")
    prepare_desktop_home(config.home())
    dest = config.inbox_dir() / safe
    dest.write_bytes(raw)
    return start_report(dest, coach)


def approve(thread_id: str) -> dict:
    review = get_review(thread_id)
    with _lock:
        if review["kind"] == "message_review":
            result = assistant_graph.resume(thread_id, True, "", _saver())
            return {
                "thread_id": thread_id,
                "status": result.get("status"),
                "persisted": result.get("persisted"),
                "message": "A person read this. Nothing was sent.",
            }
        result = report_graph.resume_run(thread_id, True, "", _saver())
    body = _present_result(thread_id, result)
    if body.get("status") == "delivered":
        body["message"] = "The PDF was saved."
    return body


def reject(thread_id: str, reason: str) -> dict:
    text = (reason or "").strip()
    if not text:
        raise ServiceError("Add a reason before rejecting.")
    if len(text) > 2000:
        raise ServiceError("The reason is too long.")
    review = get_review(thread_id)
    with _lock:
        if review["kind"] == "message_review":
            result = assistant_graph.resume(thread_id, False, text, _saver())
            return {
                "thread_id": thread_id,
                "status": result.get("status"),
                "message": "Rejected. Nothing was sent.",
            }
        result = report_graph.resume_run(thread_id, False, text, _saver())
    body = _present_result(thread_id, result)
    body["message"] = "The report was held. Nothing was delivered."
    return body


def audit_rows(limit: int = 50) -> list[dict]:
    try:
        size = int(limit)
    except (TypeError, ValueError):
        size = 50
    size = max(1, min(size, 200))
    rows = audit.read_rows(limit=size)
    out = []
    for row in rows:
        out.append(
            {
                "id": row["id"],
                "run_at": row["run_at"],
                "thread_id": row["thread_id"],
                "kind": row["kind"],
                "outcome": row["outcome"],
                "coach": row["coach"],
                "source_file": row["source_file"],
                "pipeline_version": row["pipeline_version"],
                "reason": row["reason"],
                "artifact": row["artifact"],
            }
        )
    return out


def ask(question: str) -> dict:
    text = (question or "").strip()
    if not text:
        raise ServiceError("Type a question first.")
    session_id = f"ask-{uuid.uuid4().hex[:10]}"
    with _lock:
        result = assistant_graph.ask(text, session_id, _saver())
    body = _present_result(session_id, result)
    body["session_id"] = session_id
    if body.get("status") == "paused":
        body["message"] = "This answer is waiting for a person. Nothing was sent."
    return body


def lookup_question(question: str) -> dict:
    text = (question or "").strip()
    if not text:
        raise ServiceError("Type a question first.")
    session_id = f"lib-{uuid.uuid4().hex[:10]}"
    result = library_graph.lookup(text, session_id)
    passages = []
    for item in result.get("passages") or []:
        if isinstance(item, dict) and item.get("id"):
            passages.append(item["id"])
    return {
        "session_id": session_id,
        "answer": result.get("answer") or "",
        "status": result.get("status"),
        "passages": passages,
    }


def resolve_outbox_file(path: str) -> Path:
    """A PDF under the outbox, or an error. Nothing else may be opened."""
    if not path or not str(path).strip():
        raise ServiceError("Name the PDF to open.")
    outbox = config.outbox_dir().resolve()
    try:
        target = Path(path).expanduser().resolve()
    except OSError as exc:
        raise ServiceError("That file could not be opened.") from exc
    try:
        target.relative_to(outbox)
    except ValueError:
        raise ServiceError("Only a file in the outbox can be opened.") from None
    if not target.is_file():
        raise ServiceError("That PDF is not on this computer anymore.", status=404)
    return target


def open_outbox_file(path: str) -> dict:
    target = resolve_outbox_file(path)
    opened = _launch(target)
    return {
        "path": str(target),
        "opened": opened,
        "message": "Opened the PDF." if opened else "The PDF is saved at the path below.",
    }


def _launch(target: Path) -> bool:
    try:
        if sys.platform == "win32":
            os_start = getattr(os, "startfile", None)
            if os_start is None:
                return False
            os_start(str(target))  # type: ignore[misc]
            return True
        command = ["open", str(target)] if sys.platform == "darwin" else ["xdg-open", str(target)]
        completed = subprocess.run(command, check=False, capture_output=True)
        return completed.returncode == 0
    except Exception:
        return False
