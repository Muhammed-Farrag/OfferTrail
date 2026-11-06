"""SQLite-backed vector store with two logical collections: `candidate` and `job`.

Vectors are float32 BLOBs; search is brute-force cosine in numpy, which is plenty for a
personal volume (thousands of chunks). Only vectors produced by the active embedding model are
searched — after a model change, `reindex` re-embeds everything.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass

import numpy as np

from jobradar.db.conn import transaction
from jobradar.rag.embed import Embedder
from jobradar.util import iso


@dataclass
class Hit:
    chunk_id: str
    text: str
    score: float
    source_id: int


def add_chunks(conn: sqlite3.Connection, embedder: Embedder, collection: str,
               items: list[tuple[str, int, int, str]], job_id: int | None = None) -> int:
    """items: (chunk_id, source_id, source_version, text). Existing IDs are replaced."""
    if not items:
        return 0
    vecs = embedder.embed([t for *_, t in items])
    with transaction(conn):
        for (chunk_id, source_id, version, text), v in zip(items, vecs, strict=True):
            conn.execute(
                "INSERT OR REPLACE INTO chunks (id, collection, source_id, source_version, job_id,"
                " text, embedding, embed_model, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (chunk_id, collection, source_id, version, job_id, text,
                 v.astype("<f4").tobytes(), embedder.model_id, iso()),
            )
    return len(items)


def delete_collection_source(conn: sqlite3.Connection, collection: str, *,
                             job_id: int | None = None) -> None:
    if collection == "job" and job_id is not None:
        conn.execute("DELETE FROM chunks WHERE collection = 'job' AND job_id = ?", (job_id,))
    elif collection == "candidate":
        conn.execute("DELETE FROM chunks WHERE collection = 'candidate'")


def search(conn: sqlite3.Connection, embedder: Embedder, collection: str, query: str,
           k: int = 5, job_id: int | None = None, min_score: float = 0.0) -> list[Hit]:
    sql = ("SELECT id, text, embedding, source_id FROM chunks "
           "WHERE collection = ? AND embed_model = ?")
    args: list = [collection, embedder.model_id]
    if job_id is not None:
        sql += " AND job_id = ?"
        args.append(job_id)
    rows = conn.execute(sql, args).fetchall()
    if not rows:
        return []
    mat = np.stack([np.frombuffer(r["embedding"], dtype="<f4") for r in rows])
    q = embedder.embed([query])[0]
    scores = mat @ q
    order = np.argsort(-scores)[:k]
    return [Hit(rows[i]["id"], rows[i]["text"], float(scores[i]), rows[i]["source_id"])
            for i in order if scores[i] >= min_score]


def stale_model_count(conn: sqlite3.Connection, embedder: Embedder) -> int:
    return conn.execute("SELECT COUNT(*) FROM chunks WHERE embed_model != ?",
                        (embedder.model_id,)).fetchone()[0]


def reindex(conn: sqlite3.Connection, embedder: Embedder) -> int:
    rows = conn.execute(
        "SELECT id, collection, source_id, source_version, job_id, text FROM chunks "
        "WHERE embed_model != ?", (embedder.model_id,)).fetchall()
    for r in rows:
        add_chunks(conn, embedder, r["collection"],
                   [(r["id"], r["source_id"], r["source_version"], r["text"])], r["job_id"])
    return len(rows)
