"""The report pipeline as a LangGraph state graph.

    ingest -> validate -> [gates fail] -> hold -> END
                       -> compute_facts -> write_notes
                          -> [numbers do not check out] -> write_notes (once)
                                                        -> hold -> END
                          -> review (human interrupt)
                             -> [reject] -> hold -> END
                             -> render_pdf -> deliver -> END

Everything that stops writes an audit row on the way out.
"""

from __future__ import annotations

import json
import shutil
import sqlite3
from pathlib import Path
from typing import Any

import pandas as pd
from langgraph.checkpoint.sqlite import SqliteSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from . import audit, config, model
from .adapters import adapt
from .checks import check_all_notes
from .facts import compute_facts, file_provenance
from .gates import GateFailure, run_all_gates
from .render import render_report


class ReportState(TypedDict, total=False):
    thread_id: str
    coach: str
    source_path: str
    frame_summary: dict[str, Any]
    adapter: dict[str, Any]
    gate_failures: list[dict]
    facts: dict[str, Any]
    draft_notes: dict[str, str]
    notes_violations: list[dict]
    notes_attempts: int
    review_decision: dict[str, Any]
    pdf_path: str
    outbox_path: str
    audit_row: dict[str, Any]
    hold_note_path: str
    status: str


# --------------------------------------------------------------------------
# helpers
# --------------------------------------------------------------------------

def _load_frame(source_path: str) -> tuple[pd.DataFrame, Any]:
    raw = pd.read_csv(source_path)
    return adapt(raw)


def _summarize(df: pd.DataFrame) -> dict:
    summary: dict[str, Any] = {"rows": int(len(df)), "columns": list(df.columns)}
    if "pitcher" in df.columns:
        summary["pitchers"] = sorted(str(p) for p in df["pitcher"].dropna().unique())
    if "date" in df.columns:
        summary["dates"] = sorted(str(d) for d in df["date"].dropna().unique())
    return summary


# --------------------------------------------------------------------------
# nodes
# --------------------------------------------------------------------------

def ingest(state: ReportState) -> dict:
    source = Path(state["source_path"])
    if not source.exists():
        return {
            "status": "ingest_failed",
            "gate_failures": [
                GateFailure(
                    gate="ingest",
                    code="file_missing",
                    operator_detail=f"No file at {source}.",
                    coach_note="We could not find the file that was submitted. Please send it again.",
                ).to_dict()
            ],
        }
    try:
        df, report = _load_frame(str(source))
    except Exception as exc:  # a file that will not parse is a gate failure, not a crash
        return {
            "status": "ingest_failed",
            "gate_failures": [
                GateFailure(
                    gate="ingest",
                    code="unreadable_file",
                    operator_detail=f"pandas could not read {source.name}: {exc!r}",
                    coach_note=(
                        "This file did not open as a pitch tracking export. "
                        "Please re-export it as CSV and send it again."
                    ),
                ).to_dict()
            ],
        }
    return {
        "status": "ingested",
        "frame_summary": _summarize(df),
        "adapter": {
            "mapping": report.mapping,
            "unmapped_headers": report.unmapped_headers,
            "missing_canonical": report.missing_canonical,
        },
        "notes_attempts": 0,
    }


def validate(state: ReportState) -> dict:
    if state.get("gate_failures"):
        return {}
    df, report = _load_frame(state["source_path"])
    failures = run_all_gates(df, report)
    return {
        "gate_failures": [f.to_dict() for f in failures],
        "status": "gates_failed" if failures else "gates_passed",
    }


def route_after_validate(state: ReportState) -> str:
    return "hold" if state.get("gate_failures") else "compute_facts"


def compute_facts_node(state: ReportState) -> dict:
    df, _ = _load_frame(state["source_path"])
    provenance = file_provenance(Path(state["source_path"]), df)
    return {"facts": compute_facts(df, provenance), "status": "facts_computed"}


def write_notes(state: ReportState) -> dict:
    attempts = int(state.get("notes_attempts", 0))
    feedback = None
    if state.get("notes_violations"):
        feedback = "\n".join(
            f"- {v['pitcher']}: {v['kind']}: {v['detail']}" for v in state["notes_violations"]
        )
    writer = model.get_notes_writer()
    try:
        draft = writer(state["facts"], feedback)
    except model.ModelRefusal as exc:
        return {
            "draft_notes": {},
            "notes_attempts": attempts + 1,
            "notes_violations": [
                {"pitcher": "all", "kind": "model_refusal", "detail": str(exc)}
            ],
            "status": "notes_rejected",
        }
    violations = [v.to_dict() for v in check_all_notes(draft, state["facts"])]
    return {
        "draft_notes": draft,
        "notes_attempts": attempts + 1,
        "notes_violations": violations,
        "status": "notes_rejected" if violations else "notes_ok",
    }


def route_after_notes(state: ReportState) -> str:
    if not state.get("notes_violations"):
        return "review"
    if int(state.get("notes_attempts", 0)) < 2:
        return "write_notes"
    return "hold"


def review(state: ReportState) -> dict:
    facts = state["facts"]
    decision = interrupt(
        {
            "kind": "report_review",
            "coach": state.get("coach"),
            "source_file": facts["provenance"]["source_file"],
            "version": facts["version"],
            "provenance": facts["provenance"],
            "pitchers": sorted(facts["pitchers"]),
            "draft_notes": state.get("draft_notes", {}),
            "instructions": "Resume with {'approved': true} or {'approved': false, 'reason': '...'}",
        }
    )
    if isinstance(decision, str):
        decision = {"approved": decision.strip().lower() in ("approve", "approved", "yes", "true")}
    decision = dict(decision or {})
    decision.setdefault("approved", False)
    return {
        "review_decision": decision,
        "status": "approved" if decision["approved"] else "rejected",
    }


def route_after_review(state: ReportState) -> str:
    return "render_pdf" if state.get("review_decision", {}).get("approved") else "hold"


def render_pdf(state: ReportState) -> dict:
    out = config.state_dir() / "renders" / f"{state['thread_id']}.pdf"
    render_report(state["facts"], state.get("draft_notes", {}), out)
    return {"pdf_path": str(out), "status": "rendered"}


def deliver(state: ReportState) -> dict:
    coach = state.get("coach") or "unassigned"
    facts = state["facts"]
    dest_dir = config.outbox_dir() / coach
    dest_dir.mkdir(parents=True, exist_ok=True)
    stem = Path(facts["provenance"]["source_file"]).stem
    dest = dest_dir / f"{stem}_{state['thread_id']}.pdf"
    shutil.copyfile(state["pdf_path"], dest)

    facts_dest = dest_dir / f"{stem}_{state['thread_id']}.facts.json"
    facts_dest.write_text(json.dumps(facts, indent=2))

    row = audit.write_row(
        thread_id=state["thread_id"],
        kind="report",
        outcome="DELIVERED",
        coach=coach,
        provenance=facts["provenance"],
        reason=None,
        artifact=str(dest),
        detail={
            "pitchers": sorted(facts["pitchers"]),
            "notes_attempts": state.get("notes_attempts"),
            "review": state.get("review_decision"),
        },
    )
    return {"outbox_path": str(dest), "audit_row": row, "status": "delivered"}


def hold(state: ReportState) -> dict:
    """Write the hold note and the audit row. Nothing downstream runs."""
    source_name = Path(state.get("source_path", "unknown.csv")).name
    coach = state.get("coach") or "unassigned"
    facts = state.get("facts") or {}
    provenance = facts.get("provenance", {"source_file": source_name})

    failures = state.get("gate_failures") or []
    violations = state.get("notes_violations") or []
    decision = state.get("review_decision") or {}

    if failures:
        reason = "gates: " + ", ".join(f["code"] for f in failures)
    elif decision and not decision.get("approved", False):
        reason = "review rejected: " + (decision.get("reason") or "no reason given")
    elif violations:
        reason = "notes failed the numeric check after retry"
    else:
        reason = "held"

    lines = [
        f"# Report on hold: {source_name}",
        "",
        f"Version {config.PIPELINE_VERSION} | gates {config.RULES_VERSION}",
        f"Thread {state.get('thread_id')} | coach {coach}",
        "",
        "## For the coach",
        "",
    ]
    if failures:
        for failure in failures:
            lines.append(f"- {failure['coach_note']}")
    elif decision and not decision.get("approved", False):
        lines.append(
            "- We looked at this one before sending it and held it back. "
            + (decision.get("reason") or "It did not meet the bar we set for these reports.")
        )
        lines.append("- Nothing was sent. We will follow up with what we need.")
    else:
        lines.append(
            "- The written summary for this file did not pass our own accuracy check, "
            "so nothing was sent. This is a problem on our end, not with your data."
        )
    lines += [
        "",
        "Nothing was delivered. No partial report was sent.",
        "",
        "## For the operator",
        "",
    ]
    if failures:
        for failure in failures:
            lines.append(f"- [{failure['gate']}/{failure['code']}] {failure['operator_detail']}")
    if violations:
        for violation in violations:
            lines.append(f"- [notes/{violation['kind']}] {violation['pitcher']}: {violation['detail']}")
    if decision:
        lines.append(f"- [review] decision={json.dumps(decision)}")
    lines.append(f"- [state] attempts={state.get('notes_attempts', 0)} status={state.get('status')}")
    if state.get("adapter"):
        lines.append(f"- [adapter] {json.dumps(state['adapter'])}")

    note_path = config.errors_dir() / f"{source_name}.hold.md"
    note_path.parent.mkdir(parents=True, exist_ok=True)
    note_path.write_text("\n".join(lines) + "\n")

    row = audit.write_row(
        thread_id=state.get("thread_id", "unknown"),
        kind="report",
        outcome="HOLD",
        coach=coach,
        provenance=provenance,
        reason=reason,
        artifact=str(note_path),
        detail={"gate_failures": failures, "notes_violations": violations, "review": decision},
    )
    return {"hold_note_path": str(note_path), "audit_row": row, "status": "held"}


# --------------------------------------------------------------------------
# graph
# --------------------------------------------------------------------------

def build_graph(checkpointer=None):
    builder = StateGraph(ReportState)
    builder.add_node("ingest", ingest)
    builder.add_node("validate", validate)
    builder.add_node("compute_facts", compute_facts_node)
    builder.add_node("write_notes", write_notes)
    builder.add_node("review", review)
    builder.add_node("render_pdf", render_pdf)
    builder.add_node("deliver", deliver)
    builder.add_node("hold", hold)

    builder.add_edge(START, "ingest")
    builder.add_edge("ingest", "validate")
    builder.add_conditional_edges(
        "validate", route_after_validate, {"hold": "hold", "compute_facts": "compute_facts"}
    )
    builder.add_edge("compute_facts", "write_notes")
    builder.add_conditional_edges(
        "write_notes",
        route_after_notes,
        {"review": "review", "write_notes": "write_notes", "hold": "hold"},
    )
    builder.add_conditional_edges(
        "review", route_after_review, {"render_pdf": "render_pdf", "hold": "hold"}
    )
    builder.add_edge("render_pdf", "deliver")
    builder.add_edge("deliver", END)
    builder.add_edge("hold", END)

    return builder.compile(checkpointer=checkpointer)


def open_checkpointer(path: Path | None = None) -> SqliteSaver:
    db_path = Path(path) if path else config.checkpoint_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(db_path, check_same_thread=False)
    return SqliteSaver(conn)


def start_run(source_path: str, coach: str, thread_id: str, checkpointer=None) -> dict:
    graph = build_graph(checkpointer or open_checkpointer())
    cfg = {"configurable": {"thread_id": thread_id}}
    return graph.invoke(
        {
            "thread_id": thread_id,
            "coach": coach,
            "source_path": str(Path(source_path).resolve()),
            "notes_attempts": 0,
        },
        config=cfg,
    )


def resume_run(thread_id: str, approved: bool, reason: str = "", checkpointer=None) -> dict:
    graph = build_graph(checkpointer or open_checkpointer())
    cfg = {"configurable": {"thread_id": thread_id}}
    return graph.invoke(
        Command(resume={"approved": approved, "reason": reason}), config=cfg
    )
