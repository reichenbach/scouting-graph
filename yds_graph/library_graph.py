"""The Library graph: grounded retrieval over unstructured staff docs.

Roster, schedule and opponent facts stay on the Bench Coach graph and its
SQL tools. This graph answers questions about methodology, how a note is
written, a conference handbook excerpt, and the staff philosophy. It
retrieves passages from a local vector store, a model writes from those
passages, and checks.py proves every number. Nothing retrieved means the
answer is a refusal, not a guess.
"""

from __future__ import annotations

from typing import Any

from langgraph.graph import END, START, StateGraph
from typing_extensions import TypedDict

from . import audit, config, library, model
from .checks import check_library_answer


class LibraryState(TypedDict, total=False):
    session_id: str
    question: str
    passages: list[dict]
    answer: str
    violations: list[dict]
    attempts: int
    audit_row: dict[str, Any]
    status: str


def retrieve(state: LibraryState) -> dict:
    passages = library.retrieve(
        state["question"], sample_dir=config.sample_data_dir()
    )
    return {"passages": passages, "status": "retrieved"}


def draft(state: LibraryState) -> dict:
    attempts = int(state.get("attempts", 0))
    feedback = None
    if state.get("violations"):
        feedback = "; ".join(v["detail"] for v in state["violations"])
    writer = model.get_library_writer()
    answer = writer(state["question"], state.get("passages") or [], feedback)
    return {
        "answer": answer,
        "attempts": attempts + 1,
        "status": "drafted",
    }


def post_check(state: LibraryState) -> dict:
    violations = [
        v.to_dict()
        for v in check_library_answer(state.get("answer", ""), state.get("passages") or [])
    ]
    return {
        "violations": violations,
        "status": "check_failed" if violations else "check_passed",
    }


def route_after_check(state: LibraryState) -> str:
    if state.get("violations"):
        return "draft" if int(state.get("attempts", 0)) < 2 else "hold_answer"
    return "persist"


def persist(state: LibraryState) -> dict:
    row = audit.write_row(
        thread_id=state.get("session_id", "unknown"),
        kind="library",
        outcome="ANSWERED",
        reason=None if state.get("passages") else "nothing retrieved",
        detail={
            "passage_ids": [p["id"] for p in state.get("passages", [])],
            "sources": [p["source"] for p in state.get("passages", [])],
        },
    )
    return {"audit_row": row, "status": "done"}


def hold_answer(state: LibraryState) -> dict:
    row = audit.write_row(
        thread_id=state.get("session_id", "unknown"),
        kind="library",
        outcome="HOLD",
        reason="answer contained a number that did not trace to a retrieved passage",
        detail={"violations": state.get("violations", [])},
    )
    return {
        "answer": (
            "I could not answer that without stating a number I cannot trace to a "
            "library passage, so I am not answering. Ask the staff."
        ),
        "audit_row": row,
        "attempts": 0,
        "status": "held",
    }


def build_graph():
    builder = StateGraph(LibraryState)
    builder.add_node("retrieve", retrieve)
    builder.add_node("draft", draft)
    builder.add_node("post_check", post_check)
    builder.add_node("persist", persist)
    builder.add_node("hold_answer", hold_answer)

    builder.add_edge(START, "retrieve")
    builder.add_edge("retrieve", "draft")
    builder.add_edge("draft", "post_check")
    builder.add_conditional_edges(
        "post_check",
        route_after_check,
        {"draft": "draft", "persist": "persist", "hold_answer": "hold_answer"},
    )
    builder.add_edge("persist", END)
    builder.add_edge("hold_answer", END)
    return builder.compile()


def lookup(question: str, session_id: str) -> dict:
    graph = build_graph()
    return graph.invoke(
        {
            "session_id": session_id,
            "question": question,
            "violations": [],
            "attempts": 0,
        }
    )
