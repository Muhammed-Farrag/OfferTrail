"""Canonical job records, source mapping, duplicates and enrichment versions."""

from __future__ import annotations

import re
import sqlite3
from dataclasses import dataclass

from jobradar.db.conn import transaction
from jobradar.enrich.extract import EXTRACTOR_VERSION, JobFacts, extract_facts
from jobradar.ingest.store import invalidate_job_analysis
from jobradar.parse.alert_parser import ParsedAlert
from jobradar.parse.linkedin import canonical_linkedin_url
from jobradar.util import dumps, iso, loads, sha256_text


def _norm(s: str | None) -> str:
    return re.sub(r"[^a-z0-9]+", " ", (s or "").lower()).strip()


def canonical_key(alert: ParsedAlert, channel_id: str, message_id: int) -> str:
    if alert.linkedin_id:
        return f"linkedin:{alert.linkedin_id}"
    if alert.destination_url:
        return f"url:{alert.destination_url.split('#')[0]}"
    return f"post:{channel_id}:{message_id}"


def upsert_job_from_alert(conn: sqlite3.Connection, alert: ParsedAlert, post: sqlite3.Row
                          ) -> tuple[int, bool]:
    """Create or update the canonical job for an alert and link the source post.
    Returns (job_id, created)."""
    key = canonical_key(alert, post["channel_id"], post["message_id"])
    now = iso()
    dest = canonical_linkedin_url(alert.linkedin_id) if alert.linkedin_id else alert.destination_url
    with transaction(conn, immediate=True):
        row = conn.execute("SELECT id FROM jobs WHERE canonical_key = ?", (key,)).fetchone()
        created = row is None
        if created:
            job_id = conn.execute(
                "INSERT INTO jobs (canonical_key, linkedin_id, title, publisher, location, "
                "work_mode, destination_url, first_published_at, created_at, updated_at) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (key, alert.linkedin_id, alert.title, alert.company, alert.location,
                 alert.work_mode, dest, post["published_at"], now, now),
            ).lastrowid
        else:
            job_id = row["id"]
            # The latest version of a source post wins for alert-level fields.
            conn.execute(
                "UPDATE jobs SET title = COALESCE(?, title), publisher = COALESCE(?, publisher),"
                " location = COALESCE(?, location), work_mode = CASE WHEN ? = 'unknown' "
                "THEN work_mode ELSE ? END, updated_at = ?, first_published_at = "
                "MIN(first_published_at, ?) WHERE id = ?",
                (alert.title, alert.company, alert.location, alert.work_mode, alert.work_mode,
                 now, post["published_at"], job_id),
            )
        conn.execute("INSERT OR IGNORE INTO job_sources VALUES (?, ?, ?)",
                     (job_id, post["id"], now))
        if created:
            _record_possible_duplicates(conn, job_id, alert)
    return job_id, created


def _record_possible_duplicates(conn: sqlite3.Connection, job_id: int, alert: ParsedAlert
                                ) -> None:
    title, company = _norm(alert.title), _norm(alert.company)
    if not title:
        return
    for other in conn.execute("SELECT id, title, publisher FROM jobs WHERE id != ?", (job_id,)):
        if _norm(other["title"]) == title and _norm(other["publisher"]) == company:
            a, b = sorted((job_id, other["id"]))
            conn.execute(
                "INSERT OR IGNORE INTO possible_duplicates VALUES (?, ?, ?, ?)",
                (a, b, "same title and company, different job IDs", iso()))


@dataclass
class EnrichmentResult:
    enrichment_id: int
    version: int
    facts: JobFacts
    instructions_changed: bool


def add_enrichment(conn: sqlite3.Connection, job_id: int, *, source: str, fetch_status: str,
                   description: str | None, facts: JobFacts | None = None,
                   extractor: str = EXTRACTOR_VERSION) -> EnrichmentResult:
    """Store a new enrichment version. A fetch failure is recorded as such and leaves
    availability unknown — it is never interpreted as a closure."""
    if facts is None:
        facts = extract_facts(description) if description else JobFacts()
    instruction_hash = sha256_text(facts.apply_instructions or "")
    now = iso()
    with transaction(conn, immediate=True):
        prev = conn.execute(
            "SELECT version, instruction_hash, fetch_status FROM enrichments WHERE job_id = ? "
            "ORDER BY version DESC LIMIT 1", (job_id,)).fetchone()
        version = (prev["version"] + 1) if prev else 1
        eid = conn.execute(
            "INSERT INTO enrichments (job_id, version, source, fetch_status, description, "
            "excerpts_json, availability, seniority_text, years_min, sponsorship, "
            "remote_scope_json, apply_instructions, stated_employer, instruction_hash, extractor,"
            " retrieved_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (job_id, version, source, fetch_status, description, dumps(facts.excerpts),
             facts.availability, facts.seniority_text, facts.years_min, facts.sponsorship,
             dumps(facts.remote_scope), facts.apply_instructions, facts.stated_employer,
             instruction_hash, extractor, now),
        ).lastrowid

        if description:
            if facts.availability == "closed":
                ev = next((e["excerpt"] for e in facts.excerpts if e["field"] == "availability"),
                          "closure stated")
                conn.execute(
                    "UPDATE jobs SET availability = 'closed', availability_evidence = ?, "
                    "availability_observed_at = ?, updated_at = ? WHERE id = ?",
                    (ev, now, now, job_id))
            elif source == "fetch" and fetch_status == "ok":
                conn.execute(
                    "UPDATE jobs SET availability = 'open', availability_evidence = ?, "
                    "availability_observed_at = ?, updated_at = ? WHERE id = ?",
                    ("listing accessible; no closure notice", now, now, job_id))

        flags = set(loads(conn.execute("SELECT flags_json FROM jobs WHERE id = ?",
                                       (job_id,)).fetchone()[0], []))
        if facts.stated_employer:
            publisher = conn.execute("SELECT publisher FROM jobs WHERE id = ?",
                                     (job_id,)).fetchone()[0]
            conn.execute("UPDATE jobs SET stated_employer = ? WHERE id = ?",
                         (facts.stated_employer, job_id))
            if publisher and _norm(publisher) != _norm(facts.stated_employer):
                flags.add("publisher_employer_differs")
        conn.execute("UPDATE jobs SET flags_json = ? WHERE id = ?", (dumps(sorted(flags)), job_id))

        changed = False
        if description:  # a failed fetch adds no evidence, so existing analysis stays valid
            prev_ok = conn.execute(
                "SELECT instruction_hash FROM enrichments WHERE job_id = ? AND id != ? AND "
                "description IS NOT NULL ORDER BY version DESC LIMIT 1", (job_id, eid)).fetchone()
            changed = bool(prev_ok) and prev_ok["instruction_hash"] != instruction_hash
            invalidate_job_analysis(
                conn, job_id,
                "application instructions changed" if changed else "new job description version")
    return EnrichmentResult(eid, version, facts, changed)


def latest_enrichment(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row | None:
    """Latest enrichment that actually has a description (failed fetches are skipped)."""
    return conn.execute(
        "SELECT * FROM enrichments WHERE job_id = ? AND description IS NOT NULL "
        "ORDER BY version DESC LIMIT 1", (job_id,)).fetchone()


def get_job(conn: sqlite3.Connection, job_id: int) -> sqlite3.Row:
    row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    if row is None:
        raise KeyError(f"job {job_id} not found")
    return row


def job_source_posts(conn: sqlite3.Connection, job_id: int) -> list[sqlite3.Row]:
    return conn.execute(
        "SELECT p.* FROM source_posts p JOIN job_sources s ON s.post_id = p.id "
        "WHERE s.job_id = ? AND p.is_current = 1 ORDER BY p.published_at", (job_id,)).fetchall()


def possible_duplicates_of(conn: sqlite3.Connection, job_id: int) -> list[int]:
    return [r[0] for r in conn.execute(
        "SELECT CASE WHEN job_a = ? THEN job_b ELSE job_a END FROM possible_duplicates "
        "WHERE job_a = ? OR job_b = ?", (job_id, job_id, job_id))]
