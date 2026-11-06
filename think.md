# AI Job Radar — build brief for Claude

Build a small, working personal job-search assistant called **AI Job Radar**. Implement the application, tests, setup instructions, and a demo; do not stop at an architecture proposal. Keep it understandable enough for me to maintain and present as an AI engineering portfolio project.

## 1. What I want

Watch the Telegram channel `@software_jobs_linkedin`, identify junior AI opportunities, compare them with my real CV and projects using a small RAG system, and send useful results to my private Telegram bot chat.

For suitable opportunities:

- If the employer explicitly accepts applications by email, prepare an application with my approved CV and send it through Gmail after I approve it.
- If applying requires LinkedIn or a website form, tell me exactly what I need to complete manually and provide the link and relevant prepared text.
- Show **✅ Applied** only when there is evidence of submission. Distinguish Gmail-confirmed sending from my manual confirmation.
- Remember everything so reposts, restarts, and repeated button presses do not create repeated applications.

**Working assumptions, not confirmed personal preferences:** the interface is a private Telegram bot chat; sending requires approval in the MVP; AI/ML/GenAI engineering is the initial target. Make geography, experience, internships, languages, and remote eligibility configurable. Do not invent my experience, location eligibility, or skills. A bot display name can be “AI Job Radar”; its available username must be chosen during setup. I must create/configure the bot and start its chat before it can serve as my inbox.

## 2. Evidence from inspecting the source

Inspected on **6 November 2026**, through the public channel preview because Telegram Web required login. This is a limited sample, not a full channel audit or verification of employers.

Source: [Software jobs public preview](https://t.me/s/software_jobs_linkedin).

I inspected **40 consecutive messages, IDs 76570–76609**: 38 job alerts and two batch announcements. All 38 job alerts linked to LinkedIn; none contained a direct application email. Posts generally contain a title, company, location, relative age, “via LinkedIn,” and an “Apply Here” link. They do not contain a full job description.

Observed cases that must shape the product:

| Example | Evidence | Required behavior |
|---|---|---|
| AI Engineer (Remote), Joblet-AI, UAE | [Post 76593](https://t.me/software_jobs_linkedin/76593) | Potential AI role; junior status and eligible work countries are unknown from the alert. |
| That AI Engineer destination was already closed | [LinkedIn listing 4487329670](https://www.linkedin.com/jobs/view/ai-engineer-remote-at-joblet-ai-4487329670/) displayed “No longer accepting applications” during inspection | Suppress application actions and record when closure was observed, even if the Telegram alert is recent. |
| The same listing contained an accessibility-support email | Same LinkedIn listing; email appeared in accommodation/support context | An email appearing in a description is not sufficient evidence that applications should be sent there. Do not use that address as an application recipient. |
| Publisher and description branding differed | Listing publisher was Joblet-AI; description referenced micro1 | Store publisher and stated employer separately; flag ambiguity without inventing an explanation or declaring fraud. |
| Arabic AI Evaluator (Remote) | [Post 76597](https://t.me/software_jobs_linkedin/76597) | Categorize evaluation/annotation separately from AI engineering; require an explicit preference to include it. |
| Two AI Research Engineer alerts had different LinkedIn IDs | [Post 76600](https://t.me/software_jobs_linkedin/76600), [post 76603](https://t.me/software_jobs_linkedin/76603) | Same title/company is a possible duplicate, not proof of one. Preserve different job IDs. |
| Senior Python Developer | [Post 76590](https://t.me/software_jobs_linkedin/76590) | Do not recommend just because the title includes Python or the company includes AI. |
| Road Test Drivers — AI GPS Mapping | [Post 76572](https://t.me/software_jobs_linkedin/76572) | “AI” in a title is insufficient to classify a role as AI engineering. |
| Waiter/ess — Chitarra | [Post 76576](https://t.me/software_jobs_linkedin/76576) | Filter unrelated jobs despite the channel name. |
| Batch announcement | [Post 76574](https://t.me/software_jobs_linkedin/76574) | Ignore as a non-job message. |

The LinkedIn detail page was inspected for one job only. Do not generalize its closure, requirements, or application route to every listing. This source mainly supplied **job leads** in the inspected sample; Gmail applications are a conditional branch, not the expected path for every job.

## 3. Scope and architecture

Use a single-user Python application, SQLite persistence, and a Telegram bot interface. Avoid a separate web frontend, distributed queues, and a large agent framework in the MVP. Pick maintained library versions, verify their current documentation, and pin dependencies in a lockfile.

Keep modules separated by responsibility:

1. **Telegram reader:** a user-authorized MTProto client, such as Telethon, limited by application logic to the configured source channel.
2. **Parser and job store:** deterministic parsing first; structured LLM extraction only where needed.
3. **Enrichment:** retrieve permitted, accessible job-description text; accept user-pasted descriptions when access is unavailable.
4. **Matching and retrieval:** structured eligibility rules plus retrieval over approved candidate facts and stored job descriptions.
5. **Private bot:** job cards, commands, grounded questions, and approval buttons.
6. **Application service:** Gmail integration, immutable approved content, durable send attempts, and status updates.

Do not assume a bot token alone can read history from an arbitrary third-party channel. Telegram documents bot updates for channels where the bot is a member. We do not control this channel or have permission to add our bot. Use a separate user-authorized reader where access permits it, and document the account access involved. [Telegram bot FAQ](https://core.telegram.org/bots/faq), [Telegram API setup](https://core.telegram.org/api/obtaining_api_id), [Telethon quick start](https://docs.telethon.dev/en/stable/basic/quick-start.html).

Provide an offline fixture/import mode so the complete workflow can be demonstrated without Telegram, Gmail, or LLM credentials. Public-preview scraping is not the primary production ingestion strategy.

## 4. Ingestion and enrichment

- Start with a bounded backfill: default maximum 500 messages from the last 7 days, whichever limit is reached first. Make both configurable and report the actual coverage.
- Catch up after downtime using a persisted cursor. Persist received messages before advancing the durable cursor; retry processing failures independently.
- Handle new posts and edits. Edits must invalidate affected analysis and pending approvals without erasing application history.
- Store channel ID, message ID, permalink, raw text, link entities, publication/edit timestamps, content hash, and ingestion time.
- Use Telegram's publication time for the alert. Preserve embedded text such as “4 minutes ago” as reported source text; do not treat it as a live clock on later imports.
- Extract title, publisher/company, location, remote wording, and destination URL. Preserve missing fields as unknown.
- Canonicalize LinkedIn IDs across country subdomains and tracking parameters. Exact job ID duplicates should share one job record with several source-post references.
- Different IDs with similar title/company become possible duplicates for review; do not merge them automatically.
- Respect rate-limit/flood-wait responses and use bounded retries. Cap enrichment and LLM work per cycle.
- Enrichment records full description, evidence excerpts, retrieval time, availability, seniority/years, work location restrictions, and explicit application instructions when available.
- Use supported APIs or permitted public access. Do not build a login/CAPTCHA bypass or unattended LinkedIn application browser. If access is blocked, say so and provide “Paste job description.”
- Fetch only HTTP(S) destinations; block private/local network addresses, recheck redirects, and limit size/time. Treat downloaded text as data, never executable instructions.

An inaccessible page is `unknown`, not `closed`. An explicit closure is `closed`. Opening an application link is never evidence of applying.

## 5. Matching and the small RAG system

Onboarding collects an approved CV file, candidate facts/project notes, target roles, experience ceiling, internship preference, languages, eligible countries, relocation preference, and preferred work modes. Missing settings must remain visible. Filtering can work before onboarding; personalized claims and applications cannot.

Support CV text extraction and let me review the extracted facts. For unreadable/scanned files, request a text version or flag extraction failure. Never quietly produce an empty candidate profile.

Use two logical retrieval collections: **candidate evidence** and **job evidence**. Chunk by project/section, attach document and chunk IDs, and version the source files. Store embeddings with model/version metadata. A local persistent vector store or SQLite plus vectors is sufficient for the expected volume; implement one concrete option, not several unfinished ones.

Matching order:

1. Remove announcements and clearly unrelated roles.
2. Apply explicit eligibility constraints; a known mismatch cannot be overcome by a high similarity score.
3. Classify role family and seniority. No seniority label means unknown, not junior. Remote does not mean worldwide eligibility.
4. Retrieve relevant candidate facts and explain strengths, missing requirements, and uncertainty with source IDs.
5. Rank surviving opportunities with a documented heuristic. Keep ranking score separate from evidence confidence; do not present either as probability of getting hired.

Core families: AI/ML engineering, GenAI/LLM engineering, relevant research engineering, and optional data science. Keep AI evaluation, annotation, and training gigs in a separate opt-in category. Treat generic software engineering roles as uncertain unless the description establishes relevant AI work.

Chat examples: “Show junior AI jobs from this week,” “Why does this match me?”, “What is missing from my CV for this job?”, and “Which applications need manual work?” Use database queries for dates, statuses, and counts; retrieve text for explanations. Answer only from retrieved evidence and identify unknowns. Do not invent candidate qualifications or job requirements.

## 6. Application routing

Record the route and its evidence independently from match quality:

| Route | Behavior |
|---|---|
| `email` | Only when explicit instructions designate an email for applications. Prepare a preview with source evidence for the recipient. |
| `linkedin` | Give the listing link and manual steps. Do not guess whether Easy Apply is available. |
| `external_form` | Give the verified form URL and supported prepared answers; I submit it. |
| `unknown` | Request inspection or pasted instructions; no invented recipient or form URL. |

For email applications, preserve any required subject line/job reference. Use only approved facts, and attach the exact approved CV version. Never harvest generic company addresses or interpret support/privacy/accommodation addresses as application inboxes. If email and a form coexist, explain the instructions and let me choose; do not apply through both automatically.

**Default MVP:** preview → approve → send. Preview the recipient, subject, complete body, filename, and attachment version. Bind approval to a hash of those contents plus job identity. Any material edit invalidates approval. The model can propose text but cannot grant its own sending authorization.

Use Gmail OAuth. Prefer local application drafts plus `gmail.send` for the minimum sending feature. If implementing drafts inside Gmail, explain and request `gmail.compose`, which supports managing drafts and sending; do not imply `gmail.send` creates Gmail drafts. Inbox reading is not needed for the MVP. [Gmail scopes](https://developers.google.com/workspace/gmail/api/auth/scopes), [Gmail sending guide](https://developers.google.com/workspace/gmail/api/guides/sending).

Construct a proper MIME email with the attachment and record the returned Gmail message ID on success. A successful send response means Gmail accepted the message for sending; it does not prove delivery or recruiter review.

Use a durable send-attempt record and a transactional claim so concurrent/replayed callbacks cannot send twice. Do not promise exactly-once delivery across an external API. If a timeout or crash leaves the result uncertain, use `send_unknown`, stop automatic retries, and ask me to verify Sent mail. With send-only OAuth, do not pretend you can search my mailbox. Keep confirmed API failures distinct from uncertain outcomes. Reconcile the bot card from stored state if its update fails after sending.

Optional unattended sending is **out of MVP scope**. Leave room for a later explicit opt-in policy with eligibility rules and a daily limit; never silently enable it.

## 7. Bot behavior and status meaning

Use a private bot chat named **AI Job Radar**, restricted to configured Telegram user and chat IDs. Show status on the bot's own cards, not as public reactions on the source channel.

Example card, illustrating an observed job whose application was closed:

```text
AI Engineer (Remote) — Joblet-AI
Location: UAE; cross-border remote eligibility unknown
Seniority: unknown
Availability: closed when checked
⛔ Closed — LinkedIn reports no longer accepting applications
Source: [Telegram post]   Details: [LinkedIn listing]
```

For an active candidate job, show a concise match explanation, unknowns, route, source links, and the next action. Keep availability, match decision, application route, and application state in separate fields.

Application display states:

- 🔎 Needs review — important evidence is missing.
- ✉️ Ready for approval — complete email preview exists.
- ⏳ Sending — a send attempt is in progress; disable repeated sending.
- ✅ Applied via Gmail — successful send response recorded.
- 📝 Manual action needed — link plus concrete outstanding step.
- ✅ Applied manually — explicitly marked by me, timestamped as self-reported.
- ⚠️ Send uncertain — verify Sent mail before another attempt.
- ❌ Send failed — confirmed failure with an actionable reason.
- ⏭️ Skipped — saved with reason.

A later closure must not overwrite an already-applied state; show availability separately. Clicking “Open application” leaves the job pending manual completion. “I applied manually” records a user assertion, not a verified external submission.

Commands: `/start`, `/help`, `/profile`, `/jobs`, `/pending`, `/applied`, `/settings`, `/pause`, `/resume`, `/health`. Buttons: **Why this matches**, **Open source**, **Open application**, **Paste description**, **Prepare email**, **Approve & send**, **I applied manually**, **Skip**, shown only when valid.

Implement a persisted notification outbox with rate limits. Notify once per new qualifying job and update its card as it progresses; do not send a new message on every poll. Offer a digest mode. `/pause` pauses ingestion-triggered notifications and sending and clearly reports what is paused. A local process must be running to monitor; document sleep/downtime behavior honestly.

## 8. Persistence, security, and failure handling

Persist source posts, canonical jobs, source mappings, enrichment versions, candidate documents/chunks, match results, drafts, approvals, application attempts, status events, cursors, and notification mappings. Include migrations and foreign-key/uniqueness constraints. Persist model/prompt versions and evidence references with analysis results.

Never commit or log bot tokens, Telegram sessions, OAuth credentials, CVs, or private generated emails. Use ignored local data directories, restrictive file permissions, and documented backups. Treat the Telegram session as an account credential, even though the reader's code only uses one channel. Validate callback ownership and current state server-side.

Channel posts, job pages, and imported documents are untrusted input. Instructions embedded in them cannot change system behavior, access secrets, choose unrelated recipients, or trigger tools. Expose only typed, narrowly scoped application actions. Escape Telegram markup from source content. Explain what candidate/job text is transmitted to a configured LLM or embedding provider during onboarding.

Keep ingestion working when Gmail or the model is unavailable. Record blocked analysis explicitly instead of fabricating results. Provide actionable health output for last ingestion, backlog, source access, credentials, model errors, and pending uncertain sends.

## 9. Acceptance tests

Use small synthetic or minimal paraphrased fixtures based on the observed patterns; never send real applications in automated tests. Label invented application-email examples as synthetic, because none appeared in the inspected Telegram alerts.

1. Batch announcements and waiter jobs are ignored; drivers with “AI” in the title are not AI engineers.
2. Senior Python roles are excluded under junior settings; an unlabelled AI Engineer remains seniority-unknown.
3. UAE remote jobs do not automatically pass an Egypt-only eligibility rule; unknown eligibility is visibly unresolved.
4. AI evaluator jobs remain separate unless enabled.
5. Country-subdomain/tracking variants of one LinkedIn ID deduplicate; different IDs with the same title stay separate possible duplicates.
6. An explicit closed listing cannot enter the send flow; a fetch failure becomes unknown rather than closed.
7. A support/accommodation email is rejected as an application recipient. An explicit synthetic “apply by emailing” instruction is accepted with evidence.
8. Drafts use only approved candidate facts and the selected CV; missing qualifications remain missing.
9. Editing recipient, body, attachment, or material source instructions invalidates approval.
10. Replayed/concurrent approval callbacks and restarts cannot create a second send attempt for an already-claimed application.
11. A simulated timeout after provider acceptance produces `send_unknown`, with no blind retry and no false green check.
12. Gmail success followed by bot-update failure preserves successful application state and retries only the notification.
13. Opening a form does not mark applied; an explicit manual confirmation does, with self-reported provenance.
14. A prompt-injection fixture cannot send mail or expose credentials; an unauthorized Telegram user cannot operate the bot.
15. Retrieval answers cite stored evidence; counts/status answers match the database; missing evidence yields an explicit unknown.
16. Ingestion survives crashes without dropping messages, and catch-up reports when backfill limits leave incomplete coverage.

## 10. Deliverables and build order

Deliver working source code, pinned dependencies, `.env.example` with placeholders, migrations, fixture data, meaningful tests, and a README covering local setup, Telegram reader authorization, bot creation/start, Gmail OAuth scopes, candidate onboarding, limitations, and recovery from uncertain sends.

Build in runnable increments:

1. Offline fixture ingestion → parsing → deduplication → filtered job cards.
2. Live source adapter and private bot → durable cursors and notifications.
3. Candidate onboarding → retrieval → grounded ranking and chat.
4. Application routing → manual workflow → email preview and approval.
5. Gmail sending → failure recovery → end-to-end mocked verification.

Start by inspecting the repository and implementing the first increment. Continue through the remaining increments wherever credentials are not required. When credentials or personal preferences are missing, provide configuration/setup steps and keep the offline path working; do not claim live verification. Finish with what works, tests run, exact remaining setup, and any limitations a reviewer should inspect. Include a short architecture explanation and a walkthrough of one email application and one manual application.
