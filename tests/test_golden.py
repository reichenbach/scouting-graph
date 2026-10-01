"""The golden set: five inputs, and the outcome each one must produce.

Every case runs offline on the deterministic stub model. No API key, no
network. If a change to the gates, the post-check or the library lets any of
these cases end differently, CI goes red.

- clean sample: passes every gate and the numeric check, then pauses for a
  person to review it. Nothing is delivered without that review.
- bad schema: held at validate, because a required column is missing.
- thin coverage: held at validate, because there are too few games and pitches.
- uncited number: the draft states a number with no fact id. The numeric
  post-check rejects it, the model gets one retry, and the report is held.
- library miss: retrieval returns nothing, so the answer is a refusal that
  states no number (cite-or-stop).
"""

from __future__ import annotations

import pytest

from yds_graph import audit, checks, library_graph, model, report_graph


def _uncited_writer(facts_sheet, feedback=None):
    """The stub's honest draft, plus one sentence with a number and no fact id."""
    out = model.stub_write_notes(facts_sheet, feedback)
    for pitcher in out:
        out[pitcher] += " He also worked ahead in the count 61 percent of the time."
    return out


def _outbox_files(home):
    outbox = home / "outbox"
    return list(outbox.rglob("*")) if outbox.exists() else []


def _run_report(home, checkpointer, monkeypatch, source, writer=None):
    if writer is not None:
        monkeypatch.setattr(model, "get_notes_writer", lambda: writer)
    return report_graph.start_run(str(home / source), "sample", "golden", checkpointer)


GOLDEN = [
    # (case id, input, expected outcome, where it stops, the reason code it must carry)
    ("clean_sample", "inbox/sample.csv", "pass", "review", None),
    ("bad_schema", "sample_data/bad_schema.csv", "hold", "validate", "missing_columns"),
    ("thin_coverage", "sample_data/thin_coverage.csv", "hold", "validate", "too_few_games"),
    ("uncited_number", "inbox/sample.csv", "hold", "write_notes", "uncited_number"),
    ("library_miss", "When do we play Opponent A?", "refuse", "retrieve", None),
]


@pytest.mark.parametrize(
    "case, source, expected, stage, code", GOLDEN, ids=[g[0] for g in GOLDEN]
)
def test_golden(home, checkpointer, monkeypatch, case, source, expected, stage, code):
    if case == "library_miss":
        result = library_graph.lookup(source, "golden")
        assert result["passages"] == [], "a miss must retrieve nothing"
        assert checks.numbers_in(result["answer"]) == [], "a refusal states no number"
        assert "nothing on file" in result["answer"].lower()
        row = audit.read_rows(path=home / "audit.sqlite")[0]
        assert row["reason"] == "nothing retrieved"
        return

    writer = _uncited_writer if case == "uncited_number" else None
    result = _run_report(home, checkpointer, monkeypatch, source, writer)

    if expected == "pass":
        assert result["status"] == "notes_ok"
        assert result["notes_violations"] == []
        assert result["__interrupt__"], "a passing run still waits for a person"
        assert _outbox_files(home) == []
        return

    assert expected == "hold"
    assert result["status"] == "held"
    assert "__interrupt__" not in result
    assert _outbox_files(home) == []
    assert audit.read_rows(path=home / "audit.sqlite")[0]["outcome"] == "HOLD"
    if stage == "validate":
        assert result.get("facts") is None
        assert code in {f["code"] for f in result["gate_failures"]}
    else:
        assert result["notes_attempts"] == 2, "exactly one retry, then hold"
        assert code in {v["kind"] for v in result["notes_violations"]}
