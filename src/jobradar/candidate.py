"""Candidate onboarding: approved CV versions and reviewable candidate facts.

Only facts the user approved are indexed for retrieval or used in drafts. Extraction that
yields no text is reported as a failure — the profile is never silently empty.
"""

from __future__ import annotations

import os
import shutil
import sqlite3
from dataclasses import dataclass
from pathlib import Path

from jobradar.db.conn import transaction
from jobradar.rag.chunk import facts_from_text
from jobradar.rag.embed import Embedder
from jobradar.rag.store import add_chunks, delete_collection_source
from jobradar.util import iso, sha256_bytes


class ExtractionFailed(Exception):
    pass


def extract_text(path: Path) -> str:
    suffix = path.suffix.lower()
    if suffix == ".pdf":
        from pypdf import PdfReader

        reader = PdfReader(str(path))
        return "\n".join((page.extract_text() or "") for page in reader.pages).strip()
    if suffix == ".docx":
        import docx

        d = docx.Document(str(path))
        return "\n".join(p.text for p in d.paragraphs).strip()
    if suffix in (".txt", ".md", ".markdown"):
        return path.read_text(encoding="utf-8").strip()
    raise ExtractionFailed(f"unsupported file type {suffix!r}; provide PDF, DOCX, TXT or MD")


@dataclass
class ImportResult:
    document_id: int
    version: int
    status: str
    fact_count: int
    duplicate: bool = False


def import_document(conn: sqlite3.Connection, documents_dir: Path, src: Path, kind: str
                    ) -> ImportResult:
    data = src.read_bytes()
    digest = sha256_bytes(data)
    existing = conn.execute("SELECT id, version, extraction_status FROM candidate_documents "
                            "WHERE sha256 = ? AND kind = ?", (digest, kind)).fetchone()
    if existing:
        n = conn.execute("SELECT COUNT(*) FROM candidate_facts WHERE document_id = ?",
                         (existing["id"],)).fetchone()[0]
        return ImportResult(existing["id"], existing["version"], existing["extraction_status"], n,
                            duplicate=True)

    try:
        text = extract_text(src)
        status = "ok" if len(text) >= 40 else "empty"
    except ExtractionFailed:
        raise
    except Exception as e:  # corrupt / scanned / encrypted files
        text, status = "", f"failed:{type(e).__name__}"

    documents_dir.mkdir(parents=True, exist_ok=True)
    os.chmod(documents_dir, 0o700)
    with transaction(conn, immediate=True):
        version = conn.execute("SELECT COALESCE(MAX(version), 0) + 1 FROM candidate_documents "
                               "WHERE kind = ?", (kind,)).fetchone()[0]
        stored = documents_dir / f"{kind}_v{version}_{digest[:8]}{src.suffix.lower()}"
        shutil.copyfile(src, stored)
        os.chmod(stored, 0o600)
        doc_id = conn.execute(
            "INSERT INTO candidate_documents (kind, filename, stored_path, sha256, version, "
            "extracted_text, extraction_status, created_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (kind, src.name, str(stored), digest, version, text, status, iso()),
        ).lastrowid
        facts = facts_from_text(text) if status == "ok" else []
        for section, fact in facts:
            conn.execute("INSERT INTO candidate_facts (document_id, section, text) VALUES (?,?,?)",
                         (doc_id, section, fact))
    return ImportResult(doc_id, version, status, len(facts))


def list_facts(conn: sqlite3.Connection, only_approved: bool = False) -> list[sqlite3.Row]:
    sql = ("SELECT f.*, d.kind, d.version FROM candidate_facts f "
           "JOIN candidate_documents d ON d.id = f.document_id")
    if only_approved:
        sql += " WHERE f.approved = 1"
    return conn.execute(sql + " ORDER BY f.id").fetchall()


def review_facts(conn: sqlite3.Connection, approve: list[int], reject: list[int] = ()) -> None:
    now = iso()
    with transaction(conn):
        for fid in approve:
            conn.execute("UPDATE candidate_facts SET approved = 1, reviewed_at = ? WHERE id = ?",
                         (now, fid))
        for fid in reject:
            conn.execute("UPDATE candidate_facts SET approved = 0, reviewed_at = ? WHERE id = ?",
                         (now, fid))


def edit_fact(conn: sqlite3.Connection, fact_id: int, text: str) -> None:
    conn.execute("UPDATE candidate_facts SET text = ?, approved = 1, reviewed_at = ? WHERE id = ?",
                 (text.strip(), iso(), fact_id))


def approve_cv(conn: sqlite3.Connection, document_id: int) -> None:
    row = conn.execute("SELECT kind, extraction_status FROM candidate_documents WHERE id = ?",
                       (document_id,)).fetchone()
    if row is None or row["kind"] != "cv":
        raise ValueError("not a CV document")
    conn.execute("UPDATE candidate_documents SET approved_at = ? WHERE id = ?",
                 (iso(), document_id))


def approved_cv(conn: sqlite3.Connection) -> sqlite3.Row | None:
    """The newest approved CV version — the one attached to email applications."""
    return conn.execute(
        "SELECT * FROM candidate_documents WHERE kind = 'cv' AND approved_at IS NOT NULL "
        "ORDER BY version DESC LIMIT 1").fetchone()


def index_candidate(conn: sqlite3.Connection, embedder: Embedder) -> int:
    """Rebuild the candidate collection from approved facts only."""
    facts = list_facts(conn, only_approved=True)
    with transaction(conn):
        delete_collection_source(conn, "candidate")
    items = [(f"cand:f{f['id']}", f["id"], f["version"],
              f"{f['section']}: {f['text']}" if f["section"] else f["text"]) for f in facts]
    return add_chunks(conn, embedder, "candidate", items)


def profile_status(conn: sqlite3.Connection) -> dict:
    cv = approved_cv(conn)
    total = conn.execute("SELECT COUNT(*) FROM candidate_facts").fetchone()[0]
    approved = conn.execute("SELECT COUNT(*) FROM candidate_facts WHERE approved = 1"
                            ).fetchone()[0]
    failed = conn.execute("SELECT filename, extraction_status FROM candidate_documents "
                          "WHERE extraction_status != 'ok'").fetchall()
    return {
        "approved_cv": dict(cv) if cv else None,
        "facts_total": total,
        "facts_approved": approved,
        "extraction_problems": [dict(r) for r in failed],
        "ready_for_personalization": bool(approved),
        "ready_for_email": bool(approved and cv),
    }
