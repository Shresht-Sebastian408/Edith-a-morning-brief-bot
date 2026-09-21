# Morning Brief

Every morning at 7am, a Telegram message tells me what actually needs my
attention: the internship email worth opening, the assignment due today, and
nothing else. There's a voice note too, so I can listen while getting ready
instead of staring at my phone.

I built it because my inbox is mostly Quora digests and my calendar is mostly
noise, and I kept missing the two emails a week that mattered.

## How it works

Small agents, one per source, each answering a single question: is anything
here worth this person's morning? A supervisor collects their answers and
writes the brief.

```
Gmail (IMAP)    ──> EmailAgent    ─┐
                                   ├──> Orchestrator ──> Telegram
Calendar (iCal) ──> CalendarAgent ─┘                     text + voice note
```

The rule that holds the whole thing together:

> Raw data never reaches the orchestrator.

Agents reduce their source to a list of `Signal` objects before handing
anything up. Sixty emails become about six signals of thirty tokens each. The
supervisor never learns what Gmail's API returns, so its context stays small no
matter how many agents I add later.

I didn't set out to test that claim. Partway through building this I had to bin
Google OAuth entirely and switch to IMAP. It took rewriting two
connector files. Every agent, rule, prompt and test was untouched, and all 28
tests at the time passed without a single edit.

### Filtering happens in three stages, cheapest first

| Stage | Cost | What it removes |
|---|---|---|
| Gmail query (`X-GM-RAW`) | free | promotions and social, server-side |
| Rules in `prefilter.py` | free | known junk senders, known priority keywords |
| The model | a few paise | only what the first two couldn't decide |

Most mail never becomes a token.

The calendar agent calls no model at all. Events arrive already structured and
timed, so there is nothing for a model to figure out, and paying for one would
buy nothing.

## Setup

Takes about fifteen minutes. No Google Cloud project, no OAuth consent screen.

### Install

```bash
python -m venv .venv
.venv/Scripts/activate          # Linux/Mac: source .venv/bin/activate
pip install -e ".[dev]"
cp .env.example .env
git config core.hooksPath .githooks
```

That last line turns on a pre-commit hook that refuses to commit anything
shaped like an API key. Worth doing before you touch `.env`.

### Gmail

I started with the Gmail API and gave up on it. Publishing an OAuth app to
production now requires a homepage, a privacy policy, and a domain you own and
verify by DNS. You cannot use a `github.io` address, because GitHub owns that
suffix, not you. Writing a privacy policy for software with one user was not a
good use of an evening.

App passwords need none of that, and they don't expire.

1. Turn on [2-Step Verification](https://myaccount.google.com/signinoptions/two-step-verification).
   App passwords don't exist without it.
2. Create one at [myaccount.google.com/apppasswords](https://myaccount.google.com/apppasswords).
3. Put it in `.env` as `GMAIL_APP_PASSWORD`, with your address as
   `GMAIL_ADDRESS`. Paste the spaces if you like, the code strips them.

Worth knowing what you're trading here. An app password can read *and send*
your mail, which is more than the read-only OAuth scope would have
granted. This code only reads. It opens the mailbox `readonly=True` and fetches
with `BODY.PEEK`, so nothing gets marked as read. But the credential itself
allows more, so treat it like a password and revoke it from that same page the
moment you stop using it.

### Calendar

In Google Calendar, hover your calendar in the sidebar, then open Settings and
sharing, scroll to "Integrate calendar", and copy the secret address in iCal
format into `CALENDAR_ICAL_URL`.

Anyone with that URL can read your calendar. There's a reset button on the same
page if it ever gets out.

### Telegram

1. Message [@BotFather](https://t.me/BotFather) and send `/newbot`. The username
   has to end in `bot`.
2. Open your new bot and press Start. This step is easy to skip and nothing
   works without it, because a Telegram bot cannot message you until you've
   messaged it.
3. Run `python scripts/get_telegram_chat_id.py` and copy the id into `.env`.

### Model

Grab a key from [aistudio.google.com/apikey](https://aistudio.google.com/apikey)
and set `GEMINI_API_KEY`.

A morning run costs roughly 4K input and 2.3K output tokens across two calls,
which lands well inside the free tier. I've never paid for it.

Gemini's flagship returns 503 more often than you'd expect. On my first real
send, every model in the chain was saturated at once. Switching models doesn't
help, since they share a backend, so each call now walks the chain three times
with 0, 6 and 20 second gaps. The retry fixed it on the next run: three 503s,
a six second wait, then the flagship answered. A cron job can afford twenty
seconds. It can't afford to skip a day.

If you'd rather have Claude write the final brief and leave triage on Gemini,
set `LLM_SYNTHESIS_PROVIDER=anthropic` and add `ANTHROPIC_API_KEY`. Nothing
else changes.

### ffmpeg, optional

Telegram only renders audio as a proper voice note if it's OGG/OPUS, and ffmpeg
does that conversion. GitHub Actions runners have it already. Locally:

```powershell
winget install Gyan.FFmpeg
```

Skip it and the brief still arrives, just as an MP3 attachment instead of a
waveform you can tap.

## Running it

```bash
python -m brief --agent email        # one connector, no model, nothing sent
python -m brief --dry-run            # whole pipeline, printed
python -m brief --dry-run --explain  # plus why each email was kept or dropped
python -m brief                      # send it
pytest                               # 49 tests, no network required
```

Start with `--agent email`. It dumps the raw triage as JSON, so when something
looks wrong you can tell immediately whether the connector or the model is at
fault.

Use `--explain` when the brief *misses* something. Stuff gets dropped quietly,
and you won't notice for weeks.

## Running it at 7am

`.github/workflows/brief.yml` fires at 01:15 UTC, which is 06:45 IST. Add each
value from `.env` as a repository secret under Settings, then Secrets and
variables, then Actions. Test it by hand before trusting the schedule:

```bash
gh workflow run brief.yml -f dry_run=true
```

GitHub's cron is UTC only. India has no daylight saving, so the offset stays
right all year. Scheduled runs also tend to fire five to twenty minutes late and
occasionally get dropped when GitHub is busy, which is why the time is set early
enough to drift toward 7am rather than past it. If missed mornings start
annoying you, move the runner. Don't rewrite the app.

## Making it yours

`src/brief/rules/prefilter.py` holds all the personal taste as plain data: junk
domains, priority keywords, senders that always matter. Add your university's
domain to `ALWAYS_KEEP_DOMAINS`. The tests in `tests/test_prefilter.py` exist to
catch you breaking it.

Two prompts control the writing. `SYSTEM` in `agents/email_agent.py` decides
what counts as important. `SYSTEM` in `orchestrator.py` decides how the brief
reads and sounds.

## Things that only broke against real mail

Worth writing down, because none of these showed up in testing.

Unstop mail was being silently dropped. My domain matching was an exact set
lookup while the comment above it claimed it handled subdomains, and Unstop
turns out to send from `unstop.news` anyway, not `unstop.com`. An email titled
"Google is hiring interns!" went straight in the bin.

Email previews were full of CSS. Stripping HTML tags leaves the contents of
`<style>` blocks behind, so triage was reading stylesheets.

The 16KB fetch limit cut newsletters off mid-`<style>`, which left the block
unclosed, which made my cleanup regex eat the entire message and return nothing.
Raised to 64KB.

The one that bothered me most: when every connector was broken, the brief
cheerfully announced "your inbox is clear". A confident
lie you'd act on is worse than an error, so it now tells you the difference
between a quiet day and a broken one.

## What's deliberately missing

I wanted WhatsApp and Instagram in this. Both turned out to be dead ends.

WhatsApp's official Cloud API cannot read personal chats or group messages at
all. It only sees messages sent to a registered business number. The unofficial
libraries work, but they violate the terms of service and get numbers banned
often enough that I'd rather not gamble my actual WhatsApp account on a
convenience feature.

Instagram personal accounts lost API access when the Basic Display API was
deprecated. Converting to a Creator account gets you the Messaging API, which
only shows DMs from people who messaged you first, so it would miss most of what
I wanted it for.

The voice note is a voice note and not a phone call because Telegram bots can't
place calls. That's an account-level capability, so a real call needs a second
Telegram account calling your first one. Maybe later.

Both of the deferred ones need a machine that's always on with a persistent
session, so they'd mean leaving GitHub Actions. Agents sit behind one interface
and delivery behind another, so neither would be a rewrite.

## Security

`.env` is gitignored and has never been committed. The pre-commit hook blocks
env files, credential files, and anything matching the shape of a Google key, a
Telegram token, or an iCal private path. Override it with `--no-verify` if it
ever cries wolf.

If you fork this, keep the repo private until you've checked your own history.
```bash
git log --all -p | grep -iE "AIza|AQ\.|private-[0-9a-f]{32}"
```
Empty output means you're fine.
