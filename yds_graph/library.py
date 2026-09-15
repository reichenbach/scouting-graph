"""A local vector store over the synthetic staff library.

Roster, schedule and opponent facts stay in SQL. This module is only for
unstructured passages a coach would ask about: methodology, how a note is
written, a conference handbook excerpt, the staff philosophy. Retrieval is
TF-IDF cosine similarity. The generate step still has to cite a passage, and
checks.py still has to prove every number.

The store is a SQLite table of chunks plus a dense float vector per chunk.
There is no hosted embedding API in the test path, so the vectors are built
here from the corpus. That is enough for a small library and it keeps
`make test` offline.
"""

from __future__ import annotations

import json
import math
import re
import sqlite3
from collections import Counter
from pathlib import Path

from . import config

TOKEN_RE = re.compile(r"[a-z0-9]+")
HEADING_SPLIT_RE = re.compile(r"(?m)^## ")

# Tiny stop list. Domain words (rate, chase, zone, series) stay in.
STOP = {
    "a",
    "an",
    "and",
    "are",
    "as",
    "at",
    "be",
    "by",
    "for",
    "from",
    "in",
    "is",
    "it",
    "of",
    "on",
    "or",
    "that",
    "the",
    "this",
    "to",
    "with",
}


def library_db_path() -> Path:
    return config.state_dir() / "library.sqlite"


def _doc_paths(sample_dir: Path | None = None) -> list[Path]:
    sample = Path(sample_dir) if sample_dir else config.sample_data_dir()
    paths: list[Path] = []
    docs = sample / "docs"
    if docs.is_dir():
        paths.extend(sorted(p for p in docs.glob("*.md") if p.is_file()))
    philosophy = sample / "philosophy.md"
    if philosophy.exists():
        paths.append(philosophy)
    return paths


def tokenize(text: str) -> list[str]:
    return [t for t in TOKEN_RE.findall(text.lower()) if t not in STOP and len(t) > 1]


def chunk_markdown(path: Path) -> list[dict]:
    """Split a markdown file on ## headings. The title block is its own chunk."""
    raw = path.read_text(encoding="utf-8")
    parts = HEADING_SPLIT_RE.split(raw)
    chunks: list[dict] = []
    intro = parts[0].strip()
    intro = re.sub(r"^#\s+", "", intro)
    if intro:
        chunks.append(
            {"source": path.name, "heading": path.stem.replace("_", " "), "text": intro}
        )
    for part in parts[1:]:
        stripped = part.strip()
        if not stripped:
            continue
        lines = stripped.split("\n", 1)
        heading = lines[0].strip()
        body = lines[1].strip() if len(lines) > 1 else ""
        text = f"{heading}. {body}".strip() if body else heading
        if len(text) < 40:
            continue
        chunks.append({"source": path.name, "heading": heading, "text": text})
    return chunks


def _vocab_and_idf(tokenized: list[list[str]]) -> tuple[list[str], dict[str, float]]:
    df: Counter[str] = Counter()
    for tokens in tokenized:
        df.update(set(tokens))
    vocab = sorted(df)
    n_docs = max(len(tokenized), 1)
    idf = {term: math.log((1 + n_docs) / (1 + df[term])) + 1.0 for term in vocab}
    return vocab, idf


def _tfidf_vector(tokens: list[str], vocab: list[str], idf: dict[str, float]) -> list[float]:
    counts = Counter(tokens)
    length = max(len(tokens), 1)
    return [(counts[term] / length) * idf[term] for term in vocab]


def _l2_normalize(vec: list[float]) -> list[float]:
    norm = math.sqrt(sum(v * v for v in vec))
    if norm == 0:
        return vec
    return [v / norm for v in vec]


def _cosine(a: list[float], b: list[float]) -> float:
    return float(sum(x * y for x, y in zip(a, b)))


def build_index(
    path: Path | None = None, sample_dir: Path | None = None
) -> Path:
    """Rebuild the library from the markdown on disk. Small corpus, so always rebuild."""
    db_path = Path(path) if path else library_db_path()
    db_path.parent.mkdir(parents=True, exist_ok=True)
    raw_chunks: list[dict] = []
    for doc in _doc_paths(sample_dir):
        raw_chunks.extend(chunk_markdown(doc))
    if not raw_chunks:
        raise RuntimeError("the staff library has no markdown to index")

    tokenized = [tokenize(ch["text"]) for ch in raw_chunks]
    vocab, idf = _vocab_and_idf(tokenized)
    for index, chunk in enumerate(raw_chunks, start=1):
        chunk["id"] = f"c{index}"
        chunk["vector"] = _l2_normalize(_tfidf_vector(tokenized[index - 1], vocab, idf))

    conn = sqlite3.connect(db_path)
    with conn:
        conn.execute("DROP TABLE IF EXISTS chunks")
        conn.execute("DROP TABLE IF EXISTS meta")
        conn.execute(
            "CREATE TABLE chunks ("
            "id TEXT PRIMARY KEY, source TEXT, heading TEXT, text TEXT, vector TEXT)"
        )
        conn.execute("CREATE TABLE meta (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('vocab', ?)", (json.dumps(vocab),)
        )
        conn.execute(
            "INSERT INTO meta (key, value) VALUES ('idf', ?)", (json.dumps(idf),)
        )
        for chunk in raw_chunks:
            conn.execute(
                "INSERT INTO chunks (id, source, heading, text, vector) VALUES (?,?,?,?,?)",
                (
                    chunk["id"],
                    chunk["source"],
                    chunk["heading"],
                    chunk["text"],
                    json.dumps(chunk["vector"]),
                ),
            )
    conn.close()
    return db_path


def _load(path: Path | None = None) -> tuple[list[dict], list[str], dict[str, float]]:
    db_path = Path(path) if path else library_db_path()
    if not db_path.exists():
        build_index(db_path)
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    rows = conn.execute(
        "SELECT id, source, heading, text, vector FROM chunks ORDER BY id"
    ).fetchall()
    vocab = json.loads(
        conn.execute("SELECT value FROM meta WHERE key = 'vocab'").fetchone()[0]
    )
    idf = json.loads(
        conn.execute("SELECT value FROM meta WHERE key = 'idf'").fetchone()[0]
    )
    conn.close()
    chunks = [
        {
            "id": row["id"],
            "source": row["source"],
            "heading": row["heading"],
            "text": row["text"],
            "vector": json.loads(row["vector"]),
        }
        for row in rows
    ]
    return chunks, vocab, idf


def retrieve(
    question: str,
    *,
    k: int | None = None,
    min_score: float | None = None,
    path: Path | None = None,
    sample_dir: Path | None = None,
) -> list[dict]:
    """Return the top passages for a question, or an empty list.

    An empty list is a real answer: the library does not have a passage, so
    the graph must refuse rather than guess.
    """
    db_path = Path(path) if path else library_db_path()
    if sample_dir is not None or not db_path.exists():
        build_index(db_path, sample_dir=sample_dir)
    chunks, vocab, idf = _load(db_path)
    query_tokens = tokenize(question)
    query = _l2_normalize(_tfidf_vector(query_tokens, vocab, idf))
    content_terms = {t for t in query_tokens if len(t) >= config.LIBRARY_OVERLAP_MIN_LEN}
    ranked = []
    for chunk in chunks:
        score = _cosine(query, chunk["vector"])
        if score < (min_score if min_score is not None else config.LIBRARY_MIN_SCORE):
            continue
        if content_terms and not content_terms.intersection(tokenize(chunk["text"])):
            continue
        ranked.append(
            {
                "id": chunk["id"],
                "source": chunk["source"],
                "heading": chunk["heading"],
                "text": chunk["text"],
                "score": round(score, 4),
            }
        )
    ranked.sort(key=lambda row: row["score"], reverse=True)
    return ranked[: k if k is not None else config.LIBRARY_TOP_K]


def render_passages(chunks: list[dict]) -> str:
    if not chunks:
        return "(no passages retrieved)"
    blocks = []
    for chunk in chunks:
        blocks.append(
            f"[{chunk['id']}] {chunk['source']} / {chunk['heading']}\n{chunk['text']}"
        )
    return "\n\n".join(blocks)
