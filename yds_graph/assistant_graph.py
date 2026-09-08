"""A small grounded staff assistant.

Same doctrine as the report pipeline, applied to a conversation:

- Retrieval is SQL over a database built from the sample files. No vector
  store, no similarity guessing.
- Every number in the final answer must appear in a tool result. Code checks
  that after the model answers, not by asking the model to be careful.
- Anything shaped like a message to parents, players or staff stops at a
  human interrupt and is never sent by this program.
- A question or answer that touches injury or eligibility sets a retention
  flag, and the note is not written to the database.
"""

from __future__ import annotations

import json
import re
import sqlite3
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt
from typing_extensions import TypedDict

from . import audit, config, model, tools
from .checks import check_answer_numbers


SYSTEM = """You are an assistant to a college baseball coaching staff.

Ground rules, checked in code after you answer:
- Any number in your answer must have come back from a tool in this
  conversation. Do not compute, average, or estimate. If you need a number,
  call a tool for it.
- Use query_roster and query_schedule for anything about our own team.
  Use opponent_facts for anything about an opponent pitcher.
- If a tool returns nothing, say that you have nothing on file. Do not fill
  the gap.
- If the staff asks for a message to parents, players or staff, call
  draft_message. You are not able to send anything, and you should say so.
- Plain sentences. No markdown, no dashes for punctuation.
- Injury and eligibility details are not written down by this system. You may
  discuss availability, but say that the detail stays with the staff."""

RETENTION_PATTERNS = re.compile(
    r"\b(injur\w*|hurt|surgery|concussion|medical|eligib\w*|redshirt|academic\w*|"
    r"suspend\w*|sprain\w*|strain\w*|is out|out for the|out this)\b",
    re.IGNORECASE,
)

POSITION_WORDS = {
    "shortstop": "SS",
    "short stop": "SS",
    "second base": "2B",
    "second baseman": "2B",
    "third base": "3B",
    "third baseman": "3B",
    "first base": "1B",
    "catcher": "C",
    "center field": "CF",
    "centerfield": "CF",
    "left field": "LF",
    "right field": "RF",
}


class AssistantState(TypedDict, total=False):
    session_id: str
    question: str
    history: list[dict]
    tool_calls: list[dict]
    answer: str
    violations: list[dict]
    attempts: int
    needs_review: bool
    review_decision: dict[str, Any]
    retention: str
    persisted: bool
    audit_row: dict[str, Any]
    status: str


# --------------------------------------------------------------------------
# the agent step
# --------------------------------------------------------------------------

def _stub_agent(question: str, history: list[dict], db_path: Path | None, fabricate: bool) -> dict:
    """A deterministic stand in for the model, so tests need no network."""
    q = question.lower()
    calls: list[dict] = []

    def call(name: str, arguments: dict) -> str:
        result = tools.execute(name, arguments, db_path)
        calls.append({"name": name, "arguments": arguments, "result": result})
        return result

    answer_parts: list[str] = []

    message_kind = None
    if "parent" in q:
        message_kind = "parents"
    elif "players" in q or "team message" in q:
        message_kind = "players"
    elif "staff message" in q or "message to staff" in q:
        message_kind = "staff"

    if message_kind:
        schedule = call(
            "query_schedule",
            {"sql": "SELECT date, opponent, site, travel FROM schedule ORDER BY date LIMIT 3"},
        )
        points = [
            "Weekend series details are in the schedule the staff sent.",
            "Travel plans have not changed.",
            "Questions about your own son go to the coach directly.",
        ]
        draft = call("draft_message", {"kind": message_kind, "points": points})
        answer_parts.append(
            f"I put together a draft for {message_kind}. Nothing has been sent and nothing will be "
            "sent by me. It is waiting for a coach to read it."
        )
        answer_parts.append("The draft uses the next dates on file and nothing else.")
    elif any(word in q for word in POSITION_WORDS) or "who can play" in q or "cover" in q:
        position = next((POSITION_WORDS[w] for w in POSITION_WORDS if w in q), "SS")
        rows = call(
            "query_roster",
            {
                "sql": (
                    "SELECT player, positions_played, bats, throws, notes FROM roster "
                    f"WHERE positions_played LIKE '%{position}%'"
                )
            },
        )
        try:
            parsed = json.loads(rows)
        except json.JSONDecodeError:
            parsed = []
        names = ", ".join(r["player"] for r in parsed) or "nobody on the roster"
        answer_parts.append(
            f"On file, these players have taken reps at {position}: {names}."
        )
        answer_parts.append(
            "That is what the roster records, not a depth chart. The staff decides the order."
        )
        answer_parts.append("Availability detail stays with the staff and is not written down here.")
    else:
        pitchers = tools.list_pitchers(db_path)
        schedule = call(
            "query_schedule",
            {"sql": "SELECT date, opponent, site, travel FROM schedule ORDER BY date LIMIT 3"},
        )
        target = pitchers[0] if pitchers else None
        facts_text = call("opponent_facts", {"pitcher": target}) if target else ""
        answer_parts.append("Here is what is on file for the next series.")
        answer_parts.append(f"Schedule rows on file: {schedule}")
        if facts_text:
            first_line = [
                line for line in facts_text.splitlines() if line.startswith("[")
            ][:2]
            answer_parts.append(
                f"For {target}, the facts sheet reads: " + " ".join(first_line) + "."
            )
        answer_parts.append("Those are the only numbers on file for that pitcher.")

    if fabricate:
        answer_parts.append("He also throws his slider 91.7 percent of the time in two strike counts.")

    return {"answer": " ".join(answer_parts), "tool_calls": calls}


def _live_agent(question: str, history: list[dict], db_path: Path | None) -> dict:
    import anthropic

    client = model._client()
    messages: list[dict] = []
    for turn in history:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})

    calls: list[dict] = []
    response = None
    for _ in range(8):
        response = model._create(
            client,
            model=config.MODEL_ID,
            max_tokens=config.MAX_TOKENS,
            thinking={"type": "adaptive"},
            system=SYSTEM,
            tools=tools.TOOL_SPECS,
            messages=messages,
        )
        if response.stop_reason == "refusal":
            raise model.ModelRefusal(str(response.stop_details))
        if response.stop_reason != "tool_use":
            break
        messages.append({"role": "assistant", "content": response.content})
        results = []
        for block in response.content:
            if block.type != "tool_use":
                continue
            arguments = dict(block.input)
            result = tools.execute(block.name, arguments, db_path)
            calls.append({"name": block.name, "arguments": arguments, "result": result})
            results.append(
                {"type": "tool_result", "tool_use_id": block.id, "content": result}
            )
        messages.append({"role": "user", "content": results})

    text = "".join(b.text for b in (response.content if response else []) if b.type == "text")
    return {"answer": text.strip(), "tool_calls": calls}


# --------------------------------------------------------------------------
# The same loop against a local model, without native tool calling
#
# A small local model has no tool_use block, so the protocol is written into
# the prompt: the model emits one JSON action per turn and code executes it.
# Everything downstream is unchanged. The post check still proves every
# number, and a message to parents still stops at the same interrupt.
# --------------------------------------------------------------------------

MAX_LOCAL_STEPS = 6

REACT_PROTOCOL = """
You work in a loop. Every turn you answer with exactly one JSON object and
nothing else. No prose outside it, no code fence.

To use a tool:
{"tool": "query_schedule", "args": {"sql": "SELECT date, opponent FROM schedule ORDER BY date LIMIT 3"}}

To answer the staff, once you have what you need:
{"final": "Your answer in plain sentences."}

The tools you may call, and their arguments:
%s

Rules for the loop:
- One JSON object per turn. Never both a tool and a final.
- A tool result comes back to you as a message beginning TOOL RESULT.
- Every number in your final answer must appear in a tool result you already
  received this conversation. If you do not have a number, do not state one.
- You have at most %d turns. Call a tool only when you need it.
"""


def _tool_menu() -> str:
    lines = []
    for spec in tools.TOOL_SPECS:
        params = ", ".join(sorted(spec["input_schema"].get("properties", {})))
        description = " ".join(spec["description"].split())
        lines.append(f"- {spec['name']}({params}): {description}")
    return "\n".join(lines)


def _local_system() -> str:
    return SYSTEM + "\n" + (REACT_PROTOCOL % (_tool_menu(), MAX_LOCAL_STEPS))


def _parse_action(text: str) -> dict:
    """Read one action out of a local model's turn.

    Tolerant about the wrapper and about which key the model used for the
    arguments, strict about what it means: a tool call names a tool, a final
    carries text, and anything else is not an action.
    """
    payload = model.extract_json_object(text)
    if "final" in payload and payload["final"]:
        return {"kind": "final", "text": str(payload["final"])}
    if "answer" in payload and payload["answer"]:
        return {"kind": "final", "text": str(payload["answer"])}

    known = {spec["name"] for spec in tools.TOOL_SPECS}
    name = payload.get("tool") or payload.get("name") or payload.get("action")
    if not name and len(payload) == 1:
        # Small models often answer {"query_roster": {"sql": ...}} instead of
        # naming the key "tool". That is the same intent, so it is read, and
        # the tool still has to be one that exists.
        only_key = next(iter(payload))
        if only_key in known:
            arguments = payload[only_key]
            return {
                "kind": "tool",
                "name": only_key,
                "args": arguments if isinstance(arguments, dict) else {},
            }
    if not name:
        raise ValueError("action names no tool and carries no final")
    if str(name) not in known:
        raise ValueError(f"{name!r} is not one of {sorted(known)}")
    arguments = (
        payload.get("args")
        if payload.get("args") is not None
        else payload.get("arguments")
        if payload.get("arguments") is not None
        else payload.get("tool_input", {})
    )
    if isinstance(arguments, str):
        try:
            arguments = model.extract_json_object(arguments)
        except ValueError:
            arguments = {"sql": arguments}
    if not isinstance(arguments, dict):
        arguments = {}
    return {"kind": "tool", "name": str(name), "args": arguments}


def _openai_agent(question: str, history: list[dict], db_path: Path | None) -> dict:
    messages: list[dict] = []
    for turn in history:
        messages.append({"role": turn["role"], "content": turn["content"]})
    messages.append({"role": "user", "content": question})

    calls: list[dict] = []
    answer = ""
    for step in range(MAX_LOCAL_STEPS):
        last = step == MAX_LOCAL_STEPS - 1
        text = model.openai_chat(
            messages, system=_local_system(), json_object=True
        )
        messages.append({"role": "assistant", "content": text})
        try:
            action = _parse_action(text)
        except (ValueError, KeyError, TypeError) as exc:
            messages.append(
                {
                    "role": "user",
                    "content": (
                        f"That was not a valid action ({exc}). Answer again with one "
                        'JSON object, either {"tool": ..., "args": {...}} or '
                        '{"final": "..."}.'
                    ),
                }
            )
            continue

        if action["kind"] == "final":
            answer = action["text"].strip()
            break

        result = tools.execute(action["name"], action["args"], db_path)
        calls.append({"name": action["name"], "arguments": action["args"], "result": result})
        follow_up = f"TOOL RESULT ({action['name']}):\n{result}"
        if last:
            follow_up += (
                "\n\nThat was your last tool call. Answer now with "
                '{"final": "..."} using only numbers from the tool results above.'
            )
        messages.append({"role": "user", "content": follow_up})

    if not answer:
        # The loop ran out without a final. Say so rather than passing along a
        # half formed action as if it were an answer.
        answer = (
            "I could not put an answer together from what is on file, so I am not "
            "stating one. Ask the staff."
        )
    return {"answer": answer, "tool_calls": calls}


def get_agent():
    mode = config.stub_mode()
    if mode == "fabricate":
        return lambda q, h, db: _stub_agent(q, h, db, fabricate=True)
    if mode:
        return lambda q, h, db: _stub_agent(q, h, db, fabricate=False)
    backend = config.model_backend()
    if backend == config.BACKEND_STUB:
        return lambda q, h, db: _stub_agent(q, h, db, fabricate=False)
    if backend == config.BACKEND_OPENAI_COMPAT:
        return _openai_agent
    return _live_agent


# --------------------------------------------------------------------------
# nodes
# --------------------------------------------------------------------------

def _db_path(state: AssistantState) -> Path | None:
    return None


def agent(state: AssistantState) -> dict:
    attempts = int(state.get("attempts", 0))
    question = state["question"]
    if state.get("violations"):
        question = (
            question
            + "\n\nYour previous answer was rejected because these numbers did not trace to a "
            "tool result: "
            + "; ".join(v["detail"] for v in state["violations"])
            + ". Answer again using only numbers a tool returned."
        )
    runner = get_agent()
    result = runner(question, state.get("history", []), _db_path(state))

    needs_review = any(
        call["name"] == "draft_message"
        and str(call["arguments"].get("kind", "")).lower() in tools.MESSAGE_KINDS
        for call in result["tool_calls"]
    )
    text_for_retention = state["question"] + " " + result["answer"]
    retention = (
        "do_not_store" if RETENTION_PATTERNS.search(text_for_retention) else "store"
    )
    return {
        "answer": result["answer"],
        "tool_calls": result["tool_calls"],
        "attempts": attempts + 1,
        "needs_review": needs_review,
        "retention": retention,
        "status": "answered",
    }


def post_check(state: AssistantState) -> dict:
    grounded = [call["result"] for call in state.get("tool_calls", [])]
    violations = [v.to_dict() for v in check_answer_numbers(state.get("answer", ""), grounded)]
    return {
        "violations": violations,
        "status": "check_failed" if violations else "check_passed",
    }


def route_after_check(state: AssistantState) -> str:
    if state.get("violations"):
        return "agent" if int(state.get("attempts", 0)) < 2 else "hold_answer"
    return "review" if state.get("needs_review") else "persist"


def review(state: AssistantState) -> dict:
    drafts = [
        call["result"]
        for call in state.get("tool_calls", [])
        if call["name"] == "draft_message"
    ]
    decision = interrupt(
        {
            "kind": "message_review",
            "session_id": state.get("session_id"),
            "question": state.get("question"),
            "answer": state.get("answer"),
            "drafts": drafts,
            "note": "This program does not send messages. Approval records that a human read it.",
            "instructions": "Resume with {'approved': true} or {'approved': false, 'reason': '...'}",
        }
    )
    if isinstance(decision, str):
        decision = {"approved": decision.strip().lower() in ("approve", "approved", "yes", "true")}
    decision = dict(decision or {})
    decision.setdefault("approved", False)
    return {"review_decision": decision, "status": "reviewed"}


def persist(state: AssistantState) -> dict:
    retention = state.get("retention", "store")
    persisted = False
    if retention == "store":
        conn = sqlite3.connect(config.assistant_db_path())
        conn.executescript(tools.SCHEMA)
        with conn:
            conn.execute(
                "INSERT INTO assistant_notes (created_at, session_id, question, answer) "
                "VALUES (?,?,?,?)",
                (
                    datetime.now(timezone.utc).isoformat(timespec="seconds"),
                    state.get("session_id"),
                    state.get("question"),
                    state.get("answer"),
                ),
            )
        conn.close()
        persisted = True

    history = list(state.get("history", []))
    history.append({"role": "user", "content": state["question"]})
    history.append({"role": "assistant", "content": state.get("answer", "")})

    row = audit.write_row(
        thread_id=state.get("session_id", "unknown"),
        kind="assistant",
        outcome="ANSWERED" if not state.get("needs_review") else "REVIEWED",
        coach=None,
        reason=None if retention == "store" else "retention: not stored",
        detail={
            "tools_called": [c["name"] for c in state.get("tool_calls", [])],
            "needs_review": bool(state.get("needs_review")),
            "review": state.get("review_decision"),
            "retention": retention,
            "persisted": persisted,
        },
    )
    return {
        "persisted": persisted,
        "history": history,
        "violations": [],
        "attempts": 0,
        "audit_row": row,
        "status": "done",
    }


def hold_answer(state: AssistantState) -> dict:
    row = audit.write_row(
        thread_id=state.get("session_id", "unknown"),
        kind="assistant",
        outcome="HOLD",
        reason="answer contained a number that did not trace to a tool result",
        detail={"violations": state.get("violations", [])},
    )
    return {
        "answer": (
            "I could not answer that without stating a number I cannot trace to a source, "
            "so I am not answering. Ask the staff."
        ),
        "audit_row": row,
        "attempts": 0,
        "status": "held",
    }


def build_graph(checkpointer=None):
    builder = StateGraph(AssistantState)
    builder.add_node("agent", agent)
    builder.add_node("post_check", post_check)
    builder.add_node("review", review)
    builder.add_node("persist", persist)
    builder.add_node("hold_answer", hold_answer)

    builder.add_edge(START, "agent")
    builder.add_edge("agent", "post_check")
    builder.add_conditional_edges(
        "post_check",
        route_after_check,
        {"agent": "agent", "review": "review", "persist": "persist", "hold_answer": "hold_answer"},
    )
    builder.add_edge("review", "persist")
    builder.add_edge("persist", END)
    builder.add_edge("hold_answer", END)
    return builder.compile(checkpointer=checkpointer)


def ask(question: str, session_id: str, checkpointer=None) -> dict:
    from .report_graph import open_checkpointer

    tools.build_db()
    graph = build_graph(checkpointer or open_checkpointer())
    cfg = {"configurable": {"thread_id": session_id}}
    return graph.invoke(
        {"session_id": session_id, "question": question, "violations": [], "attempts": 0},
        config=cfg,
    )


def resume(session_id: str, approved: bool, reason: str = "", checkpointer=None) -> dict:
    from .report_graph import open_checkpointer

    graph = build_graph(checkpointer or open_checkpointer())
    cfg = {"configurable": {"thread_id": session_id}}
    return graph.invoke(Command(resume={"approved": approved, "reason": reason}), config=cfg)
