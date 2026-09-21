# Autonomous Morning Briefing Assistant

A supervisor agent coordinates specialist subagents that triage your inbox and
calendar overnight, then delivers a morning brief to Telegram as text plus a
playable voice note.

## The architecture, in one rule

> **Raw data never reaches the orchestrator.**

Each agent owns one noisy source and reduces it to a short list of `Signal`
objects (`src/brief/contracts.py`). A 60-email inbox reaches the supervisor as
roughly six signals of about 30 tokens each.

That single constraint is what makes the multi-agent split pay for itself. The
orchestrator's context stays small and flat as agents are added, because it
never learns what Gmail's API actually returns. Adding a WhatsApp agent later
costs the supervisor nothing.

```
Gmail  ──> EmailAgent    ─┐
                          ├─> Orchestrator ──> Brief ──> Telegram (text + voice)
Calendar ──> CalendarAgent ┘
```

Each agent runs a three-stage funnel, cheapest filter first:

| Stage | Cost | What it does |
|---|---|---|
| Source query | free | Gmail excludes promotions/social server-side |
| Deterministic rules | free | Drops known noise, promotes known priorities |
| LLM judgment | pennies | Only the ambiguous middle that survived |

The calendar agent is deliberately **LLM-free** — events arrive already
structured and timed, so there is no ambiguity worth paying a model to resolve.

## Setup

### 1. Install

```bash
python -m venv .venv
.venv/Scripts/activate        # Windows;  source .venv/bin/activate on Linux/Mac
pip install -e ".[dev]"
cp .env.example .env
```

### 2. Google (Gmail + Calendar)

In [Google Cloud Console](https://console.cloud.google.com/):

1. Enable the **Gmail API** and **Google Calendar API**.
2. **OAuth consent screen** → External → add yourself as a test user →
   **click "PUBLISH APP"** so the status reads *In production*.

   > This step is not optional. While the status is *Testing*, Google expires
   > every refresh token after exactly 7 days and your brief dies each week with
   > an opaque `invalid_grant`. Publishing does **not** require verification for
   > personal use under 100 users — you click through one "unverified app"
   > warning during the next step.

3. **Credentials** → Create OAuth client ID → **Desktop app**. Put the id and
   secret in `.env`.
4. Run the consent flow and copy the printed token into `.env`:

```bash
python scripts/bootstrap_google_auth.py
```

Scopes are read-only (`gmail.readonly`, `calendar.readonly`). This system never
sends mail, modifies a calendar, or marks anything as read.

### 3. Telegram

1. Message [@BotFather](https://t.me/BotFather), send `/newbot`, copy the token
   into `.env`.
2. Open a chat with your new bot and **send it any message** — bots cannot
   message you first.
3. `python scripts/get_telegram_chat_id.py` and copy the id into `.env`.

### 4. ffmpeg (optional locally, automatic in CI)

Telegram renders audio as a playable voice note only if it is OGG/OPUS, and
ffmpeg does that transcode. GitHub Actions runners already have it, so this
matters only for local testing:

```powershell
winget install Gyan.FFmpeg     # then restart your terminal
```

Without it the brief still sends — you get an MP3 attachment instead of a
voice note, and a warning in the log saying so.

### 5. Model provider

Get a key at [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
and set `GEMINI_API_KEY`. The free tier covers this workload comfortably: a
morning run is about 4K input and 2.3K output tokens across both calls, and the
free tier allows hundreds of requests per day against the two to six this uses.

Google AI Pro also grants $10/month of Cloud credits usable for the Gemini API,
but they must be activated manually via one.google.com and google.dev. You will
not need them for this.

To route the final brief through Claude instead while triage stays on Gemini,
set `LLM_SYNTHESIS_PROVIDER=anthropic` and `ANTHROPIC_API_KEY`. Only the
provider changes; no code does.

## Running it

```bash
python -m brief --agent calendar     # one connector, no LLM, no send
python -m brief --agent email
python -m brief --dry-run            # full pipeline, printed not sent
python -m brief --dry-run --explain  # also show why each email was kept/dropped
python -m brief --no-voice           # send text only
python -m brief                      # for real
pytest                               # rule tests
```

`--explain` is the one to reach for when the brief misses something. Silent
filtering is how a brief quietly starts being wrong.

## Scheduling

`.github/workflows/brief.yml` runs at `01:15 UTC` (06:45 IST). Add every key in
`.env` as a repository secret under **Settings → Secrets and variables →
Actions**, then trigger a manual run before trusting the cron:

```bash
gh workflow run brief.yml -f dry_run=true
```

Two things to know about Actions cron: it is UTC-only (IST has no DST, so the
offset stays correct year-round), and scheduled runs are routinely 5–20 minutes
late and occasionally dropped under platform load. The schedule is set early to
drift toward 07:00 rather than past it. If missed mornings become annoying, the
fix is moving the runner, not rewriting the app.

## Tuning it

`src/brief/rules/prefilter.py` holds your taste as editable data — noise
domains, priority keywords, always-keep senders. Add your university domain to
`ALWAYS_KEEP_DOMAINS`. `tests/test_prefilter.py` is the guard rail for edits
there.

The brief's voice lives in two prompts: `SYSTEM` in `agents/email_agent.py`
(what counts as important) and `SYSTEM` in `orchestrator.py` (how it is
written and spoken).

## What is not here, and why

| Source | Status | Reason |
|---|---|---|
| WhatsApp | deferred | The official Cloud API cannot read personal chats or groups — it only sees messages sent to a registered *business* number. The unofficial route (Baileys/whatsapp-web.js) is a ToS violation with a well-documented ban rate. |
| Instagram DMs | dropped | Personal accounts lost API access with the Basic Display API deprecation. Even after converting to a Creator account, the Messaging API only exposes DMs sent to you *after* someone messages first. |
| A ringing phone call | deferred | Telegram **bots cannot place calls** — that is an MTProto user-account capability. A real call needs a *second* Telegram account (you cannot call yourself) driving `pytgcalls`. The voice note is the 95% version with none of that. |

Both deferred items need an always-on host with a persistent session, so they
would force a move off GitHub Actions. Because agents sit behind one interface
(`agents/base.py`) and delivery behind another, neither is a rewrite.
