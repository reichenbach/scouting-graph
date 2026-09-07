from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture
def home(tmp_path, monkeypatch):
    """A throwaway repo root, so tests never touch the real outbox or audit."""
    (tmp_path / "inbox").mkdir()
    shutil.copytree(REPO / "sample_data", tmp_path / "sample_data")
    shutil.copyfile(REPO / "inbox" / "sample.csv", tmp_path / "inbox" / "sample.csv")
    monkeypatch.setenv("YDS_GRAPH_HOME", str(tmp_path))
    monkeypatch.setenv("YDS_GRAPH_STUB", "1")
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


@pytest.fixture
def checkpointer(home):
    from yds_graph.report_graph import open_checkpointer

    return open_checkpointer(home / "state" / "checkpoints.sqlite")
