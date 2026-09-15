"""What these tests prove:

- a methodology question retrieves the methodology passage, not the roster
- a roster question does not retrieve a library passage, and the answer
  names no player
- a cited answer traces every number to a retrieved passage
- an invented number is caught, retried once, and then held
- Bench Coach still answers "who plays shortstop" from SQL
"""

from __future__ import annotations

import pytest

from yds_graph import audit, assistant_graph, checks, library, library_graph, tools


def test_chase_rate_retrieves_methodology(home):
    hits = library.retrieve(
        "How is chase rate defined in a report?",
        sample_dir=home / "sample_data",
        path=home / "state" / "library.sqlite",
    )
    assert hits, "chase rate must retrieve at least one passage"
    sources = {row["source"] for row in hits}
    assert "methodology.md" in sources
    assert "roster.csv" not in sources
    combined = " ".join(row["text"].lower() for row in hits)
    assert "outside the strike zone" in combined


def test_shortstop_question_retrieves_nothing_from_the_library(home):
    hits = library.retrieve(
        "Who else has played shortstop for us?",
        sample_dir=home / "sample_data",
        path=home / "state" / "library.sqlite",
    )
    assert hits == []


def test_lookup_chase_rate_cites_a_passage_and_writes_an_audit_row(home, monkeypatch):
    monkeypatch.setenv("YDS_GRAPH_STUB", "1")
    result = library_graph.lookup("How is chase rate defined in a report?", "lib-chase")

    assert result["status"] == "done"
    assert result["violations"] == []
    assert result["passages"]
    assert "methodology.md" in {p["source"] for p in result["passages"]}
    assert checks.LIBRARY_CITATION_RE.search(result["answer"])
    assert checks.check_library_answer(result["answer"], result["passages"]) == []

    row = audit.read_rows(path=home / "audit.sqlite")[0]
    assert row["kind"] == "library"
    assert row["outcome"] == "ANSWERED"


@pytest.mark.live
def test_live_library_answer_passes_the_same_check(home, monkeypatch, capsys):
    from conftest import live_backend_or_skip

    monkeypatch.delenv("YDS_GRAPH_STUB", raising=False)
    backend = live_backend_or_skip()
    result = library_graph.lookup("How is chase rate defined in a report?", "lib-live")
    with capsys.disabled():
        print(f"\n[live] backend: {backend}")
        print(f"[live] answer: {result.get('answer')}")
        print(f"[live] passages: {[p['id'] for p in result.get('passages') or []]}")
    assert result["status"] in {"done", "held"}
    if result["status"] == "done":
        assert checks.check_library_answer(result["answer"], result["passages"]) == []
        assert result["passages"]


def test_lookup_invented_number_is_held_after_one_retry(home, monkeypatch):
    monkeypatch.setenv("YDS_GRAPH_STUB", "fabricate")
    result = library_graph.lookup("How is a weekend series structured?", "lib-fab")

    assert result["status"] == "held"
    kinds = {v["kind"] for v in result["violations"]}
    assert kinds & {"number_not_in_passage", "unknown_passage_id", "number_without_passage"}
    row = audit.read_rows(path=home / "audit.sqlite")[0]
    assert row["kind"] == "library"
    assert row["outcome"] == "HOLD"


def test_lookup_does_not_answer_a_roster_question_from_the_library(home, monkeypatch):
    monkeypatch.setenv("YDS_GRAPH_STUB", "1")
    result = library_graph.lookup(
        "Who else has played shortstop for us?", "lib-ss"
    )
    assert result["status"] == "done"
    assert result["passages"] == []
    assert "Cedar" not in result["answer"]
    assert "Oak" not in result["answer"]
    assert "nothing on file" in result["answer"].lower()


def test_bench_coach_still_answers_shortstop_from_sql(home, checkpointer):
    tools.build_db()
    graph = assistant_graph.build_graph(checkpointer)
    result = graph.invoke(
        {
            "session_id": "a-ss-sql",
            "question": "Who else has played shortstop for us?",
        },
        config={"configurable": {"thread_id": "a-ss-sql"}},
    )
    assert result["status"] == "done"
    assert any(c["name"] == "query_roster" for c in result["tool_calls"])
    assert "Cedar" in result["answer"] or "SS" in result["answer"]


def test_schedule_question_does_not_retrieve_handbook_travel(home):
    hits = library.retrieve(
        "When do we play Opponent A?",
        sample_dir=home / "sample_data",
        path=home / "state" / "library.sqlite",
    )
    assert hits == []


def test_library_citation_is_not_read_as_a_number():
    assert checks.numbers_in("Chase rate uses the zone box [c1].") == []
    assert checks.numbers_in("The zone is 0.83 feet either side [c4].") == [0.83]


def test_check_rejects_an_uncited_invented_number():
    passages = [
        {
            "id": "c1",
            "source": "methodology.md",
            "heading": "Chase rate",
            "text": "Chase rate is the share of pitches outside the zone.",
        }
    ]
    violations = checks.check_library_answer(
        "Chase rate is 41.7 percent of the time.", passages
    )
    assert any(v.kind == "uncited_number" for v in violations)
