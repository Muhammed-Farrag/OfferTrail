-- AI Job Radar initial schema.
-- Conventions: timestamps are ISO-8601 UTC strings; *_json columns hold JSON text.

-- ---------------------------------------------------------------- ingestion
CREATE TABLE source_posts (
    id            INTEGER PRIMARY KEY,
    channel_id    TEXT    NOT NULL,
    message_id    INTEGER NOT NULL,
    version       INTEGER NOT NULL DEFAULT 1,
    is_current    INTEGER NOT NULL DEFAULT 1 CHECK (is_current IN (0, 1)),
    permalink     TEXT,
    raw_text      TEXT    NOT NULL,
    links_json    TEXT    NOT NULL DEFAULT '[]',
    published_at  TEXT    NOT NULL,
    edited_at     TEXT,
    content_hash  TEXT    NOT NULL,
    ingested_at   TEXT    NOT NULL,
    kind          TEXT CHECK (kind IN ('job', 'announcement', 'unparseable')),
    processed_at  TEXT,
    process_error TEXT,
    attempts      INTEGER NOT NULL DEFAULT 0,
    UNIQUE (channel_id, message_id, version)
);
CREATE UNIQUE INDEX ux_source_posts_current
    ON source_posts (channel_id, message_id) WHERE is_current = 1;
CREATE INDEX ix_source_posts_unprocessed
    ON source_posts (processed_at) WHERE processed_at IS NULL;

CREATE TABLE ingest_cursors (
    channel_id       TEXT PRIMARY KEY,
    last_message_id  INTEGER NOT NULL,
    updated_at       TEXT    NOT NULL,
    last_run_json    TEXT    -- coverage report of the latest backfill/catch-up
);

-- ---------------------------------------------------------------- jobs
CREATE TABLE jobs (
    id               INTEGER PRIMARY KEY,
    canonical_key    TEXT NOT NULL UNIQUE,       -- e.g. linkedin:4487329670 or post:<channel>:<msg>
    linkedin_id      TEXT UNIQUE,
    title            TEXT,
    publisher        TEXT,                       -- who posted the listing (alert's company line)
    stated_employer  TEXT,                       -- employer named in the description, if different
    location         TEXT,
    work_mode        TEXT NOT NULL DEFAULT 'unknown'
                     CHECK (work_mode IN ('remote', 'hybrid', 'onsite', 'unknown')),
    destination_url  TEXT,
    role_family      TEXT,
    seniority        TEXT,
    availability     TEXT NOT NULL DEFAULT 'unknown'
                     CHECK (availability IN ('open', 'closed', 'unknown')),
    availability_evidence    TEXT,
    availability_observed_at TEXT,
    first_published_at TEXT,
    flags_json       TEXT NOT NULL DEFAULT '[]', -- e.g. publisher/employer ambiguity
    created_at       TEXT NOT NULL,
    updated_at       TEXT NOT NULL
);

-- Several Telegram posts (reposts) can point at one canonical job.
CREATE TABLE job_sources (
    job_id   INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    post_id  INTEGER NOT NULL REFERENCES source_posts (id) ON DELETE CASCADE,
    linked_at TEXT NOT NULL,
    PRIMARY KEY (job_id, post_id)
);

-- Same title/company but different job IDs: never merged automatically.
CREATE TABLE possible_duplicates (
    job_a   INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    job_b   INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    reason  TEXT NOT NULL,
    created_at TEXT NOT NULL,
    PRIMARY KEY (job_a, job_b),
    CHECK (job_a < job_b)
);

CREATE TABLE enrichments (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    version         INTEGER NOT NULL,
    source          TEXT NOT NULL CHECK (source IN ('fetch', 'paste', 'fixture')),
    fetch_status    TEXT NOT NULL,               -- ok | blocked | error:<reason>
    description     TEXT,
    excerpts_json   TEXT NOT NULL DEFAULT '[]',  -- [{field, excerpt}]
    availability    TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (availability IN ('open', 'closed', 'unknown')),
    seniority_text  TEXT,
    years_min       INTEGER,
    sponsorship     TEXT NOT NULL DEFAULT 'unknown'
                    CHECK (sponsorship IN ('yes', 'no', 'unknown')),
    remote_scope_json TEXT NOT NULL DEFAULT '{"status":"unknown"}',
    apply_instructions TEXT,
    stated_employer TEXT,
    instruction_hash TEXT,
    extractor       TEXT NOT NULL,               -- rules@v1 or rules@v1+llm:<model>@<prompt>
    retrieved_at    TEXT NOT NULL,
    UNIQUE (job_id, version)
);

-- ---------------------------------------------------------------- candidate + RAG
CREATE TABLE candidate_documents (
    id           INTEGER PRIMARY KEY,
    kind         TEXT NOT NULL CHECK (kind IN ('cv', 'notes')),
    filename     TEXT NOT NULL,
    stored_path  TEXT NOT NULL,
    sha256       TEXT NOT NULL,
    version      INTEGER NOT NULL,
    extracted_text TEXT,
    extraction_status TEXT NOT NULL,             -- ok | empty | failed:<reason>
    approved_at  TEXT,                           -- CV versions must be approved before use
    created_at   TEXT NOT NULL,
    UNIQUE (kind, version),
    UNIQUE (sha256, kind)
);

CREATE TABLE candidate_facts (
    id           INTEGER PRIMARY KEY,
    document_id  INTEGER NOT NULL REFERENCES candidate_documents (id) ON DELETE CASCADE,
    section      TEXT,
    text         TEXT NOT NULL,
    approved     INTEGER NOT NULL DEFAULT 0 CHECK (approved IN (0, 1)),
    reviewed_at  TEXT
);

CREATE TABLE chunks (
    id             TEXT PRIMARY KEY,             -- cand:f12 | job:j3e1:c0
    collection     TEXT NOT NULL CHECK (collection IN ('candidate', 'job')),
    source_id      INTEGER NOT NULL,             -- candidate_facts.id or enrichments.id
    source_version INTEGER NOT NULL,
    job_id         INTEGER REFERENCES jobs (id) ON DELETE CASCADE,
    text           TEXT NOT NULL,
    embedding      BLOB NOT NULL,                -- float32 little-endian
    embed_model    TEXT NOT NULL,                -- name@version
    created_at     TEXT NOT NULL
);
CREATE INDEX ix_chunks_collection ON chunks (collection, embed_model);

-- ---------------------------------------------------------------- matching
CREATE TABLE match_results (
    id               INTEGER PRIMARY KEY,
    job_id           INTEGER NOT NULL REFERENCES jobs (id) ON DELETE CASCADE,
    decision         TEXT NOT NULL CHECK (decision IN
                     ('candidate', 'needs_review', 'uncertain', 'excluded', 'ignored', 'blocked')),
    role_family      TEXT,
    seniority        TEXT,
    eligibility      TEXT NOT NULL,              -- eligible | excluded | unknown
    reasons_json     TEXT NOT NULL DEFAULT '[]',
    unknowns_json    TEXT NOT NULL DEFAULT '[]',
    explanation_json TEXT,                       -- strengths / missing with chunk IDs
    rank_score       REAL,
    evidence_confidence TEXT CHECK (evidence_confidence IN ('high', 'medium', 'low')),
    analyzer_version TEXT NOT NULL,
    model_version    TEXT,
    enrichment_id    INTEGER REFERENCES enrichments (id),
    created_at       TEXT NOT NULL,
    invalidated_at   TEXT,
    invalidation_reason TEXT
);
CREATE UNIQUE INDEX ux_match_current ON match_results (job_id) WHERE invalidated_at IS NULL;

-- ---------------------------------------------------------------- applications
CREATE TABLE applications (
    id            INTEGER PRIMARY KEY,
    job_id        INTEGER NOT NULL UNIQUE REFERENCES jobs (id) ON DELETE CASCADE,
    route         TEXT NOT NULL DEFAULT 'unknown'
                  CHECK (route IN ('email', 'linkedin', 'external_form', 'unknown', 'choice')),
    route_target  TEXT,                          -- recipient email or form/listing URL
    route_evidence TEXT,
    route_options_json TEXT NOT NULL DEFAULT '[]',
    state         TEXT NOT NULL CHECK (state IN (
                    'needs_review', 'ready_for_approval', 'sending', 'applied_gmail',
                    'manual_action', 'applied_manual', 'send_unknown', 'send_failed', 'skipped')),
    state_reason  TEXT,
    opened_link_at TEXT,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);

CREATE TABLE drafts (
    id               INTEGER PRIMARY KEY,
    application_id   INTEGER NOT NULL REFERENCES applications (id) ON DELETE CASCADE,
    recipient        TEXT NOT NULL,
    recipient_evidence TEXT NOT NULL,
    subject          TEXT NOT NULL,
    body             TEXT NOT NULL,
    attachment_doc_id INTEGER NOT NULL REFERENCES candidate_documents (id),
    attachment_filename TEXT NOT NULL,
    attachment_sha256 TEXT NOT NULL,
    instruction_hash TEXT NOT NULL,
    used_fact_ids_json TEXT NOT NULL DEFAULT '[]',
    content_hash     TEXT NOT NULL,
    generator        TEXT NOT NULL,
    created_at       TEXT NOT NULL,
    superseded_at    TEXT
);
CREATE UNIQUE INDEX ux_drafts_current ON drafts (application_id) WHERE superseded_at IS NULL;

CREATE TABLE approvals (
    id             INTEGER PRIMARY KEY,
    draft_id       INTEGER NOT NULL REFERENCES drafts (id) ON DELETE CASCADE,
    content_hash   TEXT NOT NULL,
    approved_by    INTEGER NOT NULL,             -- Telegram user ID or 0 for CLI
    approved_at    TEXT NOT NULL,
    invalidated_at TEXT,
    invalidation_reason TEXT
);

CREATE TABLE send_attempts (
    id               INTEGER PRIMARY KEY,
    application_id   INTEGER NOT NULL REFERENCES applications (id) ON DELETE CASCADE,
    draft_id         INTEGER NOT NULL REFERENCES drafts (id),
    approval_id      INTEGER NOT NULL REFERENCES approvals (id),
    content_hash     TEXT NOT NULL,
    state            TEXT NOT NULL CHECK (state IN ('claimed', 'sent', 'failed', 'send_unknown')),
    gmail_message_id TEXT,
    error            TEXT,
    claimed_at       TEXT NOT NULL,
    finished_at      TEXT
);
-- At most one live (claimed / sent / uncertain) attempt per application. A confirmed
-- failure frees the slot so a new, separately approved attempt can be made.
CREATE UNIQUE INDEX ux_send_attempt_live
    ON send_attempts (application_id) WHERE state IN ('claimed', 'sent', 'send_unknown');

CREATE TABLE status_events (
    id             INTEGER PRIMARY KEY,
    job_id         INTEGER REFERENCES jobs (id) ON DELETE CASCADE,
    application_id INTEGER REFERENCES applications (id) ON DELETE CASCADE,
    from_state     TEXT,
    to_state       TEXT NOT NULL,
    actor          TEXT NOT NULL CHECK (actor IN ('system', 'user', 'gmail')),
    detail         TEXT,
    at             TEXT NOT NULL
);

-- ---------------------------------------------------------------- notifications
CREATE TABLE notifications (
    id              INTEGER PRIMARY KEY,
    job_id          INTEGER NOT NULL UNIQUE REFERENCES jobs (id) ON DELETE CASCADE,
    chat_id         INTEGER,
    message_id      INTEGER,
    state           TEXT NOT NULL CHECK (state IN ('pending', 'sent', 'failed', 'digest')),
    rendered_hash   TEXT,
    attempts        INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TEXT NOT NULL,
    last_error      TEXT,
    created_at      TEXT NOT NULL,
    updated_at      TEXT NOT NULL
);

-- ---------------------------------------------------------------- runtime state
CREATE TABLE kv (
    key   TEXT PRIMARY KEY,
    value TEXT NOT NULL
);

CREATE TABLE llm_usage (
    day       TEXT PRIMARY KEY,
    requests  INTEGER NOT NULL DEFAULT 0,
    tokens    INTEGER NOT NULL DEFAULT 0,
    errors    INTEGER NOT NULL DEFAULT 0,
    last_error TEXT
);
