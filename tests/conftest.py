from __future__ import annotations

import shutil
import sys
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO))


@pytest.fixture
def home(tmp_path, monkeypatch, request):
    """A throwaway repo root, so tests never touch the real outbox or audit.

    Offline tests must never see a real key, so it is removed from the
    environment. Tests marked `live` keep it: they are the only ones that
    are supposed to call the model.
    """
    (tmp_path / "inbox").mkdir()
    shutil.copytree(REPO / "sample_data", tmp_path / "sample_data")
    shutil.copyfile(REPO / "inbox" / "sample.csv", tmp_path / "inbox" / "sample.csv")
    monkeypatch.setenv("YDS_GRAPH_HOME", str(tmp_path))
    monkeypatch.setenv("YDS_GRAPH_STUB", "1")
    if "live" not in request.keywords:
        monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    return tmp_path


@pytest.fixture
def checkpointer(home):
    from yds_graph.report_graph import open_checkpointer

    return open_checkpointer(home / "state" / "checkpoints.sqlite")


def live_backend_or_skip():
    """Skip unless a live backend is actually usable, and say which is missing.

    A live test runs against whatever `config.model_backend()` resolves to: the
    hosted model when there is a real key, or a local OpenAI compatible server
    when YDS_MODEL_BASE_URL points at one that answers.
    """
    import os

    from yds_graph import config

    config._load_env()
    backend = config.model_backend()

    if backend == config.BACKEND_STUB:
        pytest.skip("YDS_MODEL_BACKEND=stub, so there is no live model to call")

    if backend == config.BACKEND_ANTHROPIC:
        if os.environ.get("ANTHROPIC_API_KEY"):
            return config.live_backend_description()
        pytest.skip(
            "no live model configured: set ANTHROPIC_API_KEY, or set "
            "YDS_MODEL_BASE_URL and YDS_MODEL_NAME to a local OpenAI compatible server"
        )

    base_url = config.model_base_url()
    if not config.model_name():
        pytest.skip(f"YDS_MODEL_BASE_URL is {base_url} but YDS_MODEL_NAME is not set")

    import httpx

    try:
        response = httpx.get(base_url + "/models", timeout=5.0)
        response.raise_for_status()
    except Exception as exc:  # noqa: BLE001 - any failure here means "not reachable"
        pytest.skip(f"local model server at {base_url} is not reachable: {exc}")
    return config.live_backend_description()


class FakeEmbedder:
    """A deterministic stand in for a local embedding model.

    Same interface as library.EmbeddingsVectorizer, fixed small dimension, no
    network: each token is hashed into one of DIM buckets. It is not a good
    embedding. It exists so CI proves the embeddings plumbing (a stored model
    name and dimension, a query vectorized by the same thing, the same cite and
    refuse behaviour) without ollama.
    """

    DIM = 64
    name = "embeddings"
    model = "fake-hash-64"
    min_score = 0.12

    def _vec(self, text: str) -> list[float]:
        import hashlib

        from yds_graph import library

        vec = [0.0] * self.DIM
        for token in library.tokenize(text):
            bucket = int(hashlib.sha256(token.encode()).hexdigest(), 16) % self.DIM
            vec[bucket] += 1.0
        return library._l2_normalize(vec)

    def fit(self, texts):
        return [self._vec(t) for t in texts]

    def embed_query(self, text):
        return self._vec(text)

    def state(self):
        return {}

    def load_state(self, state):
        return None


@pytest.fixture(params=["tfidf", "fake_embeddings"])
def vectors(request, home, monkeypatch):
    """Run a library test once on TF-IDF and once on the fake embedder."""
    from yds_graph import library

    if request.param == "fake_embeddings":
        monkeypatch.setattr(library, "get_vectorizer", FakeEmbedder)
    else:
        monkeypatch.setenv("YDS_LIBRARY_VECTORS", "tfidf")
    return request.param
