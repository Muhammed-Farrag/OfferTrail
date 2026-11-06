# OfferTrail — AI Job Radar

A single-user job-search assistant. It watches the Telegram channel `@software_jobs_linkedin`,
filters for **junior AI/ML/GenAI roles and internships**, checks them against **your approved
CV facts** with a small local RAG system, and sends job cards to your **private Telegram bot**.
Email applications are sent through Gmail **only after you approve the exact preview**.
LinkedIn and form applications get clear manual steps.

Runs on free tiers: Groq's free LLM tier plus local embeddings. Everything also works fully
offline:

```bash
uv sync
uv run jobradar demo        # fixtures + fake LLM + fake Gmail; nothing leaves your machine
uv run pytest               # 76 tests, network disabled
```

## What it does (and refuses to do)

| It does | It refuses to |
|---|---|
| Ignore batch posts and unrelated jobs (waiters, drivers with "AI" in the title) | Treat "AI" in a company name or title as an AI-engineering role |
| Keep seniority `unknown` when not stated | Assume an unlabelled role is junior |
| Apply your eligibility rule (below) and show unresolved items | Treat "Remote" as worldwide |
| Dedupe one LinkedIn ID across subdomains/tracking params | Merge different job IDs that share a title (flags them as *possible duplicates*) |
| Record explicit closures with evidence and time | Treat a failed or blocked fetch as "closed" |
| Use an email recipient only from an explicit "apply by emailing…" sentence | Use support/accommodation/privacy addresses, or harvest generic addresses |
| Send only the approved draft, bound by a content hash | Let the model approve, choose recipients, or add skills you don't have |
| Show ✅ only with evidence (Gmail message ID, or your explicit confirmation) | Count "opened the link" as applied |
| Mark timeouts as ⚠️ *send uncertain* and stop | Blindly retry a send that may have gone out |

**Your eligibility rule** (from `data/preferences.json`, editable with `/set`):
- On-site/hybrid in **Egypt** → eligible.
- On-site/hybrid **abroad** → needs **visa sponsorship**. Stated → eligible. "No sponsorship" → excluded. Not mentioned → 🔎 *Needs review — ⚠️ visa sponsorship unknown*, ranked below confirmed jobs.
- **Remote** → must accept Egypt-based candidates (Egypt, EMEA/MENA/Africa or worldwide). Restricted elsewhere → excluded. Unknown → *Needs review*.

## Setup (≈20 minutes)

Requires Python 3.13 and [uv](https://docs.astral.sh/uv/).

```bash
uv sync
cp .env.example .env
uv run jobradar init-db
cp preferences.example.json data/preferences.json   # your stated preferences; edit as needed
```

### 1. Groq API key (free)
1. Sign in at [console.groq.com](https://console.groq.com) → **API Keys** → create a key.
2. Put it in `.env` as `JOBRADAR_GROQ_API_KEY=gsk_...`

Free-tier limits for `openai/gpt-oss-120b` are about 30 requests/min, 1,000 requests/day,
8K tokens/min and 200K tokens/day. The bot tracks a daily budget
(`JOBRADAR_LLM_DAILY_TOKEN_BUDGET`, default 150K). On rate limits it falls back to
`openai/gpt-oss-20b`. When the budget runs out it keeps working on rules only. Rule-based
filtering runs before any LLM call, so most posts cost nothing.
`JOBRADAR_LLM_BACKEND=none` keeps everything local.

### 2. Private bot (BotFather)
1. In Telegram, message **@BotFather** → `/newbot` → display name **AI Job Radar** → choose an
   available username (must end in `bot`).
2. Put the token in `.env` as `JOBRADAR_BOT_TOKEN=...`.
3. Run `uv run jobradar run --no-reader`, open your bot and press **Start**. In *setup mode*
   it replies with your numeric user ID and does nothing else.
4. Set `JOBRADAR_BOT_ALLOWED_USER_ID=<that id>` and restart. From then on the bot only answers
   you, and only in your private chat with it.

### 3. Channel reader (Telegram user session)
Bots only receive channel posts from channels they are members of. We don't control
`@software_jobs_linkedin`, so a **user** session (MTProto, via Telethon) reads it.

1. Go to [my.telegram.org](https://my.telegram.org) → **API development tools** → create an app.
   Copy `api_id` and `api_hash` into `.env`.
2. `uv run jobradar auth-telegram`: enter your phone number, the login code and your 2FA
   password if you have one.
3. This creates `data/telegram_reader.session` (mode 600). **That file is a full login to
   your Telegram account.** The code only resolves and reads the one configured channel, but
   anyone who has the file can use your account. Never commit or share it. To revoke it:
   Telegram → Settings → Devices → terminate the session.

### 4. Your CV and facts
```bash
uv run jobradar onboard --cv ~/Documents/CV.pdf --notes ~/Documents/projects.md
```
This extracts text, splits it into **facts**, and asks which ones to approve. Only approved
facts are used in explanations and emails. You can also send the CV file to the bot, then use
`/profile`, `/approvefacts` and `/approvecv`.
- A scanned or unreadable PDF is reported as an extraction failure; send a TXT/MD/DOCX version instead.
- **What leaves your machine:** embeddings are computed locally. With Groq enabled, the following
  is sent to Groq: job description text (fact extraction), your approved facts relevant to one
  job plus its title (email drafting), and your question with retrieved evidence (chat). The CV
  file itself is never sent to the LLM.

### 5. Gmail (optional, send-only)
1. In [Google Cloud Console](https://console.cloud.google.com), create a project and enable the **Gmail API**.
2. **OAuth consent screen**: User type *External*, add yourself as a **test user**.
3. **Credentials** → *Create OAuth client ID* → **Desktop app**. Download the JSON to
   `data/gmail_client_secret.json`.
4. `uv run jobradar auth-gmail`. This requests only `https://www.googleapis.com/auth/gmail.send`.
5. Set `JOBRADAR_GMAIL_SENDER=<your gmail address>`.

Notes: `gmail.send` can send mail but **cannot read your mailbox or create Gmail drafts**.
Drafts live in the local database, and uncertain sends must be checked by you in *Sent*. While
the app is in Google's *testing* mode, refresh tokens expire after about 7 days; re-run
`jobradar auth-gmail` when sending reports an auth error.

### 6. Run
```bash
uv run jobradar run
```
The first run backfills up to 500 messages from the last 7 days, whichever limit is reached
first. Later runs catch up from a saved cursor. `/health` shows the coverage, and says so if a
limit left a gap.

**Downtime:** this is a local process. While your laptop sleeps or the process is stopped,
nothing is watched. On restart it catches up from the cursor (bounded by the same limits) and
reports any gap. Cards are not lost: they are queued in the database outbox.

## Using the bot

Commands: `/start` `/help` `/jobs [week|all|uncertain]` `/job <id>` `/pending` `/applied`
`/profile` `/settings` `/set <key> <value>` `/editdraft <job> subject|body|to <text>` `/pause`
`/resume` `/health`.

Plain-text questions:
- “Show junior AI jobs from this week” → database query
- “Why does #12 match me?” → stored explanation citing your fact IDs (`cand:f3`)
- “What is missing from my CV for #12?” → requirements not found in approved facts
- “Which applications need manual work?” → database query

Buttons appear only when valid: **Why this matches**, **Open source**, **Open application**,
**Paste description**, **Prepare email**, **Approve & send**, **I applied manually**, **Skip**,
plus **Use email / Use form** when both are offered and **It is / is not in Sent mail** after
an uncertain send.

### Status meanings
| Status | Meaning |
|---|---|
| 🔎 Needs review | Important evidence is missing (seniority, eligibility, route…) |
| ✉️ Ready for approval | A complete email preview exists |
| ⏳ Sending | A send attempt is claimed; repeated taps are refused |
| ✅ Applied via Gmail | Gmail accepted the message (message ID stored). This doesn't prove delivery or that anyone read it |
| 📝 Manual action needed | Link plus concrete steps; you submit |
| ✅ Applied manually (self-reported) | You tapped *I applied manually*; timestamped, not externally verified |
| ⚠️ Send uncertain | Timeout/crash after the request left; check Sent mail before anything else |
| ❌ Send failed | Gmail definitively refused; reason shown; prepare and approve again to retry |
| ⏭️ Skipped | Saved with a reason |

Availability (`open` / `closed` / `unknown`) is a separate line. A later closure never
overwrites an applied state.

## Walkthrough: an email application
*(the synthetic “Junior Machine Learning Engineer — Nile Vision Labs” fixture in `jobradar demo`)*
1. The alert arrives. There is no description yet, so the route is LinkedIn (manual).
2. You tap **Paste description** and paste the listing. It says *“To apply, send your CV to
   careers@nilevision.example with the subject "Junior MLE - Ref ML-24"”*. That sentence is
   stored as the recipient's evidence. A *“contact help@… for accessibility”* sentence in the
   same text is rejected.
3. The card now shows **Route: Email** with the quoted evidence and **Prepare email**.
4. **Prepare email** builds the draft. The subject is kept exactly as the employer asked. The
   body comes from approved facts only: the model proposes, and the code rejects any proposal
   that cites unknown facts, claims skills you haven't approved, or contains links or addresses.
   The newest approved CV version is attached.
5. The preview shows the recipient, its evidence, the subject, the full body, the attachment
   filename/version/hash, and what generated the text.
6. **Approve & send** carries a prefix of the draft's content hash. The server re-checks the
   hash, the CV bytes, the job's current instructions and that the listing isn't closed. It
   claims the single send slot in one transaction, then calls Gmail.
7. On success, the card is edited in place to **✅ Applied via Gmail (Gmail id …)**. Tapping the
   button again does nothing.
   If it times out, the card shows **⚠️ Send uncertain** with *It is / is not in Sent mail*.

## Walkthrough: a manual (LinkedIn/form) application
*(the synthetic “Junior GenAI Engineer — Delta Prompt” fixture, Berlin, sponsorship stated)*
1. The card shows **Route: Employer form (manual)** and **📝 Manual action needed**.
2. **Open application** sends step-by-step instructions, the form link, and a short summary
   built from your approved facts. The state stays *manual action*: opening a link is not applying.
3. You submit the form yourself, then tap **I applied manually**. The state becomes
   **✅ Applied manually (self-reported)**, with your user ID and the time.

## Recovering from an uncertain send
1. Open Gmail → **Sent** and search for the recipient and subject on the card.
2. Found it → tap **✅ It is in Sent mail**. This records *applied (confirmed by you in Sent mail)*.
3. Not there → tap **❌ Not in Sent mail**. The attempt is marked failed; tap **Prepare email**
   and approve again to retry.
4. `/health` lists every unresolved uncertain send. The bot never auto-retries one.

## Limitations to review
- **Job descriptions:** alerts contain only title, company and location. Automatic page fetching is **off by default** (`JOBRADAR_FETCH_ENABLED`): check the site's terms before turning it on. It never logs in, solves CAPTCHAs or automates LinkedIn applications. Until a description is pasted or fetched, seniority, sponsorship and requirements usually stay *unknown*, so most jobs show as *Needs review*.
- **Live services not verified here:** the Telethon reader, the bot over real Telegram, Groq and Gmail are tested against mocks and the real libraries' request shapes, not live accounts. The local embedding model was run for real.
- **Gmail outcomes:** with send-only scope the bot cannot look in your mailbox, so uncertain sends need you. Exactly-once delivery across an external API is not guaranteed; at most one automatic attempt per approval is.
- **Geography:** a small built-in country/city table (`match/geo.py`). Unknown places stay unknown; add entries as needed.
- **Skill matching:** explanations match a fixed skill vocabulary (`match/explain.py`) against approved facts, plus semantic retrieval for related evidence. Synonyms outside the list may show as *missing*.
- **Single process:** one SQLite file, one event loop. A Gmail send briefly blocks the bot loop.
- **Fixtures** are short paraphrases of public posts 76570–76610 inspected on 6 Nov 2026, plus clearly marked **synthetic** cases (IDs ≥ 99000). No real application emails appeared in the inspected alerts.

## Data, secrets and backups
Everything personal lives in `data/` (git-ignored, mode 700): the SQLite DB, the Telegram
session, the Gmail token, stored CV versions and preferences. Back up with
`sqlite3 data/jobradar.sqlite3 ".backup data/backup.sqlite3"` and copy `data/` somewhere
private and encrypted. Nothing secret is logged.

See [docs/ARCHITECTURE.md](docs/ARCHITECTURE.md) for the design.
