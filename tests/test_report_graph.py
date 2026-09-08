"""What these tests prove:

- a bad export stops at the gate, writes a hold note, and delivers nothing
- thin coverage stops at the gate for the same reason
- a fabricated number in the model output is caught in code, retried once,
  and then held
- a reject at review delivers nothing
- an approve produces a PDF carrying the version stamp and provenance
"""

from __future__ import annotations

import json

import pytest

from yds_graph import audit, model, report_graph


def _audit_rows(home):
    return audit.read_rows(path=home / "audit.sqlite")


def _outbox_files(home):
    outbox = home / "outbox"
    return list(outbox.rglob("*")) if outbox.exists() else []


def canned_good(facts_sheet, feedback=None):
    """A fake model that writes only from the facts sheet it was handed."""
    out = {}
    for pitcher, meta in facts_sheet["pitchers"].items():
        chosen = [facts_sheet["facts"][i] for i in meta["fact_ids"][:4]]
        out[pitcher] = " ".join(
            f"The sample shows {f['label']} at {f['value']} [{f['id']}]." for f in chosen
        )
    return out


def canned_fabricating(facts_sheet, feedback=None):
    """A fake model that invents one number. The post-check must catch it."""
    out = canned_good(facts_sheet)
    for pitcher, meta in facts_sheet["pitchers"].items():
        anchor = facts_sheet["facts"][meta["fact_ids"][0]]
        out[pitcher] += (
            f" He held opponents to a 12.7 percent hard contact rate [{anchor['id']}]."
        )
    return out


def test_bad_schema_stops_at_validate(home, checkpointer):
    result = report_graph.start_run(
        str(home / "sample_data" / "bad_schema.csv"), "sample", "t-bad", checkpointer
    )

    assert result["status"] == "held"
    assert "__interrupt__" not in result
    assert result.get("facts") is None
    assert _outbox_files(home) == []

    note = home / "errors" / "bad_schema.csv.hold.md"
    assert note.exists()
    text = note.read_text()
    assert "velocity" in text
    assert "For the coach" in text and "For the operator" in text

    rows = _audit_rows(home)
    assert len(rows) == 1
    assert rows[0]["outcome"] == "HOLD"
    assert "missing_columns" in rows[0]["reason"]
    assert rows[0]["pipeline_version"]


def test_thin_coverage_stops_at_validate(home, checkpointer):
    result = report_graph.start_run(
        str(home / "sample_data" / "thin_coverage.csv"), "sample", "t-thin", checkpointer
    )

    assert result["status"] == "held"
    codes = {f["code"] for f in result["gate_failures"]}
    assert "too_few_games" in codes
    assert "too_few_pitches" in codes
    assert _outbox_files(home) == []
    assert _audit_rows(home)[0]["outcome"] == "HOLD"


def test_fabricated_number_is_caught_and_held_after_one_retry(home, checkpointer, monkeypatch):
    calls = {"n": 0}

    def writer(facts_sheet, feedback=None):
        calls["n"] += 1
        return canned_fabricating(facts_sheet, feedback)

    monkeypatch.setattr(model, "get_notes_writer", lambda: writer)

    result = report_graph.start_run(
        str(home / "inbox" / "sample.csv"), "sample", "t-fab", checkpointer
    )

    assert calls["n"] == 2, "the model step gets exactly one retry"
    assert result["status"] == "held"
    assert "__interrupt__" not in result
    assert _outbox_files(home) == []
    kinds = {v["kind"] for v in result["notes_violations"]}
    assert "number_not_in_facts" in kinds

    row = _audit_rows(home)[0]
    assert row["outcome"] == "HOLD"
    assert "numeric check" in row["reason"]


def test_reject_at_review_delivers_nothing(home, checkpointer, monkeypatch):
    monkeypatch.setattr(model, "get_notes_writer", lambda: canned_good)

    first = report_graph.start_run(
        str(home / "inbox" / "sample.csv"), "sample", "t-reject", checkpointer
    )
    assert first["__interrupt__"], "the graph must pause before anything is delivered"
    assert _outbox_files(home) == []

    result = report_graph.resume_run("t-reject", False, "velocity looks off in game two", checkpointer)
    assert result["status"] == "held"
    assert _outbox_files(home) == []
    note = (home / "errors" / "sample.csv.hold.md").read_text()
    assert "velocity looks off in game two" in note
    assert _audit_rows(home)[0]["outcome"] == "HOLD"


def test_approve_produces_pdf_with_provenance_footer(home, checkpointer, monkeypatch):
    monkeypatch.setattr(model, "get_notes_writer", lambda: canned_good)

    first = report_graph.start_run(
        str(home / "inbox" / "sample.csv"), "sample", "t-ok", checkpointer
    )
    payload = first["__interrupt__"][0].value
    assert set(payload["draft_notes"]) == {"OPP-11", "OPP-24"}

    result = report_graph.resume_run("t-ok", True, "", checkpointer)
    assert result["status"] == "delivered"

    pdf = home / "outbox" / "sample" / "sample_t-ok.pdf"
    assert pdf.exists()
    blob = pdf.read_bytes()
    assert b"yds-graph 0.1.0" in blob
    assert b"sample.csv" in blob
    assert b"sha256" in blob

    facts_json = json.loads((home / "outbox" / "sample" / "sample_t-ok.facts.json").read_text())
    assert facts_json["provenance"]["rows"] > 0
    assert facts_json["provenance"]["sha256"]

    row = _audit_rows(home)[0]
    assert row["outcome"] == "DELIVERED"
    assert row["source_sha256"] == facts_json["provenance"]["sha256"]


def test_resume_from_a_separate_process_uses_the_checkpointer(home):
    """A run paused in one process is resumable in another, by thread id."""
    import subprocess
    import sys

    env = {
        "PATH": "/usr/bin:/bin",
        "YDS_GRAPH_HOME": str(home),
        "YDS_GRAPH_STUB": "1",
        "PYTHONPATH": str(__import__("pathlib").Path(__file__).resolve().parent.parent),
    }
    run = subprocess.run(
        [sys.executable, "-m", "yds_graph", "run", str(home / "inbox" / "sample.csv"),
         "--coach", "sample", "--thread", "t-xproc"],
        capture_output=True, text=True, env=env,
    )
    assert run.returncode == 0, run.stderr
    assert "PAUSED FOR HUMAN REVIEW" in run.stdout

    resume = subprocess.run(
        [sys.executable, "-m", "yds_graph", "resume", "t-xproc", "--approve"],
        capture_output=True, text=True, env=env,
    )
    assert resume.returncode == 0, resume.stderr
    assert "DELIVERED" in resume.stdout


@pytest.mark.live
def test_live_write_notes_passes_the_same_check(home, monkeypatch, capsys):
    """Runs whichever real model is configured, hosted or local."""
    from conftest import live_backend_or_skip

    monkeypatch.delenv("YDS_GRAPH_STUB", raising=False)
    backend = live_backend_or_skip()

    import pandas as pd

    from yds_graph.adapters import adapt
    from yds_graph.checks import check_all_notes
    from yds_graph.facts import compute_facts, file_provenance

    source = home / "inbox" / "sample.csv"
    frame, _ = adapt(pd.read_csv(source))
    sheet = compute_facts(frame, file_provenance(source, frame))
    writer = model.get_notes_writer()
    notes = writer(sheet)
    violations = check_all_notes(notes, sheet)
    with capsys.disabled():
        print(f"\n[live] backend: {backend}")
        for pitcher in sorted(notes):
            print(f"[live] {pitcher}: {notes[pitcher]}")
    assert violations == [], violations


# --------------------------------------------------------------------------
# the documented exceptions in checks.py, and their edges
# --------------------------------------------------------------------------

def test_a_pitcher_id_is_an_identifier_not_a_number():
    from yds_graph.checks import numbers_in

    assert numbers_in("OPP-11 uses fastball 54.2% [f1] of the time.") == [54.2]
    assert numbers_in("A 0-2 count, per OPP-24, at 41.4 percent [f53].") == [41.4]
    # the exception is narrow: a bare number, or a lowercase word before the
    # hyphen, is still a claim the model has to trace
    assert numbers_in("Opponent 11 sits at 54.2 percent [f1].") == [11.0, 54.2]
    assert numbers_in("He was 11-2 in the sample [f1].") == [11.0, 2.0]


def test_naming_the_pitcher_does_not_by_itself_fail_a_draft(home):
    import pandas as pd

    from yds_graph.adapters import adapt
    from yds_graph.checks import check_notes
    from yds_graph.facts import compute_facts, file_provenance

    source = home / "inbox" / "sample.csv"
    frame, _ = adapt(pd.read_csv(source))
    sheet = compute_facts(frame, file_provenance(source, frame))
    pitcher = "OPP-11"
    facts = [sheet["facts"][i] for i in sheet["pitchers"][pitcher]["fact_ids"][:4]]
    notes = " ".join(
        f"{pitcher} shows {f['label']} at {f['value']} [{f['id']}]." for f in facts
    )
    assert check_notes(pitcher, notes, sheet) == []
