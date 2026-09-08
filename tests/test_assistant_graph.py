"""What these tests prove:

- a request for a message to parents ends at a human interrupt, never sent
- a numeric answer traces every number to a tool result
- a question that touches injury or eligibility is not written to the database
- an answer with a number no tool returned is rejected and the run holds
"""

from __future__ import annotations

import sqlite3

import pytest

from yds_graph import assistant_graph, audit, config, tools


def _notes_rows(home):
    conn = sqlite3.connect(home / "state" / "assistant.sqlite")
    try:
        return conn.execute("SELECT question, answer FROM assistant_notes").fetchall()
    finally:
        conn.close()


def test_parent_message_stops_at_review_and_is_not_sent(home, checkpointer):
    tools.build_db()
    graph = assistant_graph.build_graph(checkpointer)
    cfg = {"configurable": {"thread_id": "a-parents"}}
    result = graph.invoke(
        {"session_id": "a-parents", "question": "Draft a message to parents about the weekend."},
        config=cfg,
    )

    assert result["__interrupt__"], "a message must pause for a human"
    payload = result["__interrupt__"][0].value
    assert payload["kind"] == "message_review"
    assert "NEEDS HUMAN REVIEW" in payload["drafts"][0]
    assert "does not send messages" in payload["note"]

    # Nothing persisted while it is still paused.
    assert _notes_rows(home) == []


def test_numeric_answer_traces_to_tool_results(home, checkpointer):
    tools.build_db()
    graph = assistant_graph.build_graph(checkpointer)
    cfg = {"configurable": {"thread_id": "a-matchup"}}
    result = graph.invoke(
        {"session_id": "a-matchup", "question": "What do we have on the Friday matchup?"},
        config=cfg,
    )

    assert result["status"] == "done"
    assert result["violations"] == []

    from yds_graph.checks import check_answer_numbers, numbers_in

    grounded = [call["result"] for call in result["tool_calls"]]
    assert numbers_in(result["answer"]), "this answer is supposed to contain numbers"
    assert check_answer_numbers(result["answer"], grounded) == []


def test_injury_question_is_not_written_to_the_database(home, checkpointer):
    tools.build_db()
    graph = assistant_graph.build_graph(checkpointer)
    cfg = {"configurable": {"thread_id": "a-ss"}}
    result = graph.invoke(
        {
            "session_id": "a-ss",
            "question": "Our shortstop is out this weekend with an injury, who can play there?",
        },
        config=cfg,
    )

    assert result["retention"] == "do_not_store"
    assert result["persisted"] is False
    assert _notes_rows(home) == []

    row = audit.read_rows(path=home / "audit.sqlite")[0]
    assert row["outcome"] == "ANSWERED"
    assert row["reason"] == "retention: not stored"

    # A question with nothing sensitive in it is stored, so the test above is
    # showing the flag working rather than a database that never writes.
    graph.invoke(
        {"session_id": "a-plain", "question": "What do we have on the Friday matchup?"},
        config={"configurable": {"thread_id": "a-plain"}},
    )
    assert len(_notes_rows(home)) == 1


def test_fabricated_number_in_an_answer_is_rejected(home, checkpointer, monkeypatch):
    tools.build_db()
    monkeypatch.setenv("YDS_GRAPH_STUB", "fabricate")

    graph = assistant_graph.build_graph(checkpointer)
    cfg = {"configurable": {"thread_id": "a-fab"}}
    result = graph.invoke(
        {"session_id": "a-fab", "question": "What do we have on the Friday matchup?"},
        config=cfg,
    )

    assert result["status"] == "held"
    assert "not answering" in result["answer"]
    assert _notes_rows(home) == []
    assert audit.read_rows(path=home / "audit.sqlite")[0]["outcome"] == "HOLD"


def test_sql_tools_refuse_to_write(home):
    tools.build_db()
    assert "TOOL ERROR" in tools.execute("query_roster", {"sql": "DELETE FROM roster"})
    assert "TOOL ERROR" in tools.execute(
        "query_schedule", {"sql": "SELECT * FROM roster"}
    )
    assert "Alder" in tools.execute("query_roster", {"sql": "SELECT * FROM roster"})


@pytest.mark.live
def test_live_assistant_answer_traces_to_tools(home, monkeypatch, capsys):
    """Runs whichever real model is configured, hosted or local."""
    from conftest import live_backend_or_skip

    monkeypatch.delenv("YDS_GRAPH_STUB", raising=False)
    backend = live_backend_or_skip()

    tools.build_db()
    from yds_graph.checks import check_answer_numbers

    agent = assistant_graph.get_agent()
    result = agent(
        "What is on file for opponent pitcher OPP-11, and who is next on the schedule?",
        [],
        None,
    )
    grounded = [call["result"] for call in result["tool_calls"]]
    with capsys.disabled():
        print(f"\n[live] backend: {backend}")
        print(f"[live] tools called: {[c['name'] for c in result['tool_calls']]}")
        print(f"[live] answer: {result['answer']}")
    assert result["tool_calls"], "the assistant should have called a tool"
    assert check_answer_numbers(result["answer"], grounded) == []
