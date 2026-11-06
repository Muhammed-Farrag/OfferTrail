# Architecture

One Python process, one SQLite file, one private Telegram bot. No web frontend, queue or
agent framework.

```
 @software_jobs_linkedin                                   your private bot chat
          │ (Telethon user session)                                  ▲
          ▼                                                          │ cards / previews
 ingest/telegram_reader ──► ingest/store ──► pipeline ──► bot/outbox ┘
   backfill · catch-up      source_posts      │  parse/alert_parser + parse/linkedin
   live new/edit events     (versioned,       │  jobs (canonical, dedup, sources)
   flood-wait retries        cursor after     │  match/classify + match/rules (filter → eligibility)
                             storing)         │  enrich/extract (+ optional llm_extract, quote-verified)
                                              │  match/explain (facts ↔ requirements, chunk IDs)
                                              │  match/rank (score ≠ confidence)
                                              ▼  apply/routing (email | linkedin | form | unknown | choice)
                                         applications ──► apply/drafts ──► apply/approval ──► apply/sender ──► Gmail
                                         (state machine)   facts-only        content-hash      claim → send →
                                                           body              binding           sent/failed/unknown
```

## Modules

| Module | Responsibility |
|---|---|
| `ingest/telegram_reader.py` | Telethon client limited to the configured channel; `collect()` holds the backfill/catch-up coverage logic (tested without Telegram) |
| `ingest/store.py` | Store raw posts before moving the cursor; edits create a new version and invalidate analysis and pending approvals |
| `ingest/fixtures.py` | Offline import mode |
| `parse/` | Deterministic alert parsing; LinkedIn ID canonicalization |
| `jobs.py` | Canonical jobs, many posts → one job, possible duplicates, enrichment versions, closure evidence, publisher vs. employer flag |
| `enrich/` | SSRF-guarded fetcher (off by default), HTML→text, rule extraction with excerpts, optional LLM refinement accepted only if its quote appears in the text |
| `match/` | Family/seniority classification, eligibility (Egypt + sponsorship rule), grounded explanation, ranking |
| `rag/` | Chunking, local embeddings (fastembed bge-small; hash embedder for tests), SQLite vector store, grounded answers |
| `candidate.py` | CV/notes import, fact review, approved CV versions, candidate index (approved facts only) |
| `llm/` | Provider interface, Groq (strict JSON schema, fallback model, daily budget), fake LLM |
| `apply/` | Routing, state machine, drafts, approval binding, Gmail MIME/sender, manual workflow |
| `bot/` | Cards, outbox, `BotService` (all logic, auth, server-side state checks), PTB adapter + runtime |
| `health.py`, `cli.py`, `demo.py` | Operations |

## Key invariants

1. **Store before cursor.** A crash can re-deliver a message (deduplicated by content hash) but cannot drop one.
2. **Known mismatch is final.** Eligibility and category gates run before similarity; similarity only ranks survivors.
3. **Unknown ≠ negative.** Missing seniority, eligibility, sponsorship or availability is shown as unknown. It is not guessed, and a failed fetch never means closed.
4. **Separate fields.** Availability, match decision, route and application state are independent columns and card lines.
5. **Evidence for routes.** An email recipient requires an explicit apply-by-email sentence, stored as evidence. Support, accommodation, privacy and injection-like sentences are rejected.
6. **Approval binds content.** `sha256(recipient, subject, body, attachment sha256, filename, job id, instruction hash)`. Any edit, attachment change or change to the source instructions voids the approval.
7. **One live attempt.** A partial UNIQUE index on `send_attempts(application_id) WHERE state IN ('claimed','sent','send_unknown')`, plus an `IMMEDIATE` transaction, makes replayed or concurrent approvals fail before any network call. Stale claims become `send_unknown` on startup.
8. **The model proposes, code decides.** LLM output is schema-validated data. It can't set recipients, approve, or trigger tools. Its claims are checked against stored text (quotes, fact IDs, skill vocabulary, citations ⊆ retrieved chunks).
9. **History wins.** Applied states are terminal; later closures or edits only change the other fields.
10. **Notifications are an outbox.** One card per qualifying job, edited in place; failures back off and retry independently of the state they report.

## Ranking

`rank_score` (0–100) = family fit (≤35) + seniority fit (≤25) + eligibility (≤25, +3 when
sponsorship is stated) + share of listed skills found in approved facts (≤10) + freshness (≤5).
`evidence_confidence` (high/medium/low) counts how many of {seniority known, eligibility known,
description stored} hold. Neither number is a probability of getting hired.

## Data model

See `src/jobradar/db/migrations/0001_initial.sql`: `source_posts` (versioned), `ingest_cursors`,
`jobs`, `job_sources`, `possible_duplicates`, `enrichments`, `candidate_documents`,
`candidate_facts`, `chunks` (with `embed_model`), `match_results` (versioned with
analyzer/model versions and enrichment reference), `applications`, `drafts`, `approvals`,
`send_attempts`, `status_events`, `notifications`, `kv`, `llm_usage`.
