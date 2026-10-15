# MailLaunch

A CLI for sending personalised cold email through the **Gmail API** or **Microsoft Graph**,
rebuilt from the single-file browser tool.

What it adds over the HTML version: credentials are authorised once and stored in your OS
keychain, campaigns survive Ctrl+C and crashes and resume exactly where they stopped, every
send is written to a structured log, failures retry with exponential backoff, and the daily
limit is enforced across every campaign rather than per browser tab.

```bash
maillaunch send \
  --provider gmail \
  --csv recipients.csv \
  --subject "Hi {{name}}, quick note about {{company}}" \
  --body-file template.txt \
  --min-delay 60 \
  --max-delay 180
```

---

## Setup: zero to first send

Needs **Python 3.10+**. Takes about five minutes, most of it in the provider console.

### 1. Install

```bash
git clone <this-repo> && cd maillaunch
python -m venv .venv && source .venv/bin/activate
pip install -e .            # installs deps and the `maillaunch` command
```

`pip install -r requirements.txt` also works if you would rather run it as
`python -m src.main ...` instead of installing the command.

### 2. Register an OAuth app

You need your own OAuth client — there is no shared one to borrow.

<details>
<summary><strong>Gmail</strong></summary>

1. [Google Cloud Console](https://console.cloud.google.com/) → create or pick a project
2. **APIs & Services → Library** → enable **Gmail API**
3. **OAuth consent screen** → External → add your own address under *Test users*
4. **Credentials → Create credentials → OAuth client ID** → type **Desktop app**
5. Copy the client ID into `maillaunch.config.yaml`, and export the secret:

```bash
export MAILLAUNCH_GMAIL_CLIENT_SECRET='your-client-secret'
```
</details>

<details>
<summary><strong>Microsoft 365 / GoDaddy</strong></summary>

1. [Azure Portal](https://portal.azure.com/) → **App registrations → New registration**
2. Under **Authentication → Add a platform → Mobile and desktop applications**,
   add the redirect URI `http://localhost`
   (loopback ports are wildcarded for public clients, so the random port we bind is fine)
3. **API permissions → Microsoft Graph → Delegated → Mail.Send**
4. Copy the **Application (client) ID** into the config. No secret needed — this is a
   public client using PKCE.
</details>

### 3. Configure

```bash
cp maillaunch.config.yaml ~/.maillaunch/maillaunch.config.yaml   # or leave it in the cwd
$EDITOR ~/.maillaunch/maillaunch.config.yaml                     # paste your client id
```

### 4. Authenticate and send

```bash
maillaunch auth gmail          # opens a browser, stores tokens in your keychain
maillaunch send --csv recipients.csv \
                --subject "Hi {{name}}" \
                --body-file template.txt \
                --dry-run      # walk the whole campaign without sending anything
```

Drop `--dry-run` to send for real. **Start with the dry run** — it parses the CSV, renders
every template, reports which rows would be skipped and why, and never touches the API.

A `recipients.csv` is just:

```csv
email,name,company
john@acme.com,John Smith,"Acme, Inc."
```

---

## Commands

| Command | What it does |
|---|---|
| `maillaunch auth gmail` / `auth microsoft` | Browser OAuth, stores the refresh token |
| `maillaunch send` | Start a campaign |
| `maillaunch status` | Today's count against the limit, auth state, recent campaigns |
| `maillaunch resume` | Continue the most recent interrupted campaign |
| `maillaunch log` | Print today's send log |

`send` flags: `--provider`, `--csv`, `--subject`, `--body-file`, `--min-delay`,
`--max-delay`, `--email-column`, `--dry-run`.
`resume` takes `--campaign <id>` to pick a specific one.

A send of 100 emails at 60–180s apart takes 2–5 hours, so run it detached:

```bash
nohup maillaunch send --csv recipients.csv --subject "..." --body-file t.txt &
tail -f ~/.maillaunch/logs/$(date +%F).log
```

---

## How the important parts behave

**Resume.** Every recipient is written to SQLite before the first send, and each outcome is
committed in its own transaction as it happens. So a Ctrl+C, a crash, or a pulled power cable
costs at most the one email in flight. `resume` re-reads the `PENDING` rows — already-sent
recipients are never contacted twice, and rows that exhausted their retries stay `FAILED`
rather than being silently retried days later.

**Daily limit.** Counted as `SENT` rows for the local calendar day, across *all* campaigns and
both providers — the cap protects one human's sending reputation, which is not per-campaign.
Retries don't count: three attempts that end in one delivery is one email. On hitting the
limit the campaign is marked `paused` and `status` prints the exact resume command.

**Retries.** Non-auth failures back off 30s → 60s → 120s. A `401` is handled separately: it
refreshes the access token once and retries immediately, because sleeping does not fix an
expired credential. If the refresh itself fails, the campaign pauses and tells you to re-auth —
it does not burn through 90 recipients failing identically.

**Skips.** A recipient is logged `SKIPPED` and never contacted when the address is missing,
malformed, a duplicate of an earlier row, or when the template needs a `{{variable}}` that
row has no value for. That last one is deliberate: nobody should receive "Hi ,".

**Log.** `~/.maillaunch/logs/YYYY-MM-DD.log`, one file per day, echoed to stdout as it runs.

```
[2026-10-09T14:22:01+08:00] SENT          john@acme.com (John Smith) | campaign: c_20261009_001
[2026-10-09T14:24:38+08:00] FAILED        bob@inc.com | campaign: c_20261009_001 | error: HTTP 429 - Too Many Requests
[2026-10-09T14:26:11+08:00] RETRY         bob@inc.com | campaign: c_20261009_001 | attempt 1/2 in 30s | HTTP 429
```

---

## Tests

```bash
pip install pytest && python -m pytest tests/ -q      # 64 tests, ~12s
python -m unittest discover tests                      # or stdlib only, no pytest needed
```

No network, no credentials, no real keychain, no real `~/.maillaunch` touched.

**`tests/test_maillaunch.py` — 52 unit tests.** The five areas the brief names: CSV parsing
(BOM, quoted commas, blank rows, bad UTF-8, missing file), template rendering (missing keys,
nested braces, blank values), email column detection, daily limit enforcement, and
retry/backoff against a mocked send function. Plus resume semantics and RFC 2047 subject
encoding.

**`tests/test_integration.py` — 12 end-to-end tests** against a local HTTP server standing in
for Gmail, Graph and the OAuth token endpoint. These drive the actual CLI through whole
campaigns and assert on what went over the wire:

- the Gmail request carries a valid base64url MIME message, decoded and checked for the right
  `To:`, personalised `Subject:` and body
- the Graph request matches the JSON contract and carries no MIME blob
- a **401 mid-campaign** refreshes the token and replays that recipient with the new one
- a **429** backs off, then succeeds
- a permanent 500 is recorded `FAILED` while the rest of the list still goes out
- a failed *refresh* pauses the campaign instead of burning through the remaining recipients
- **interrupt, then resume, contacts nobody twice** — asserted on the addresses the mock
  actually received, not on database state

Both suites are mutation-checked: breaking resume so it ignores send status, or the daily
limit so it miscounts, fails 2 and 5 tests respectively.

### Verifying against the real APIs

The mock proves our logic; it cannot prove Google and Microsoft accept these request shapes.
There are no public test accounts for either — anything offered as one online is somebody's
compromised credentials. Two official tools get you a real token in about two minutes without
registering an app:

- **[Google OAuth 2.0 Playground](https://developers.google.com/oauthplayground/)** — select
  the `https://www.googleapis.com/auth/gmail.send` scope, authorise, exchange for an access
  token, then replay our exact request body against the live API.
- **[Microsoft Graph Explorer](https://developer.microsoft.com/graph/graph-explorer)** — sign
  in, consent to the delegated `Mail.Send` permission under *Modify permissions*, and POST our
  payload to `/me/sendMail`.

For a full end-to-end run, a throwaway Google account plus your own Cloud project is free and
takes five minutes. (The Microsoft 365 Developer Program sandbox is no longer a dependable
route — as of 2026 most applicants are told they don't qualify unless they hold a Visual
Studio Professional/Enterprise subscription or partner status.)

---

## Design decisions

**Python, not Node.** `sqlite3` is in the standard library, so durable campaign state costs
no dependency; `keyring` gives real OS keychain integration on both macOS and Linux; and the
brief's own reference layout is Python.

**Four dependencies.** `PyYAML`, `requests`, `keyring`, `cryptography`. The CLI is stdlib
`argparse` — six commands do not justify click or typer. State is stdlib `sqlite3`.

**Token storage: keychain first, encrypted file as fallback.** The requirement is "encrypted
local file or OS keychain; never plaintext". `keyring` is the better answer and is tried
first — but it needs a D-Bus SecretService on Linux, which a headless server or container
does not have, and a tool that only supported keychains simply would not run there. The
fallback is a Fernet-encrypted file whose key comes from `MAILLAUNCH_SECRET_KEY`, so the key
lives in the operator's environment rather than on disk next to the ciphertext. An encrypted
file with its key stored beside it is just a slower plaintext file.

**Secrets are never in the config file.** The brief's example config shows
`gmail.client_secret`, but the paragraph under it says secrets must not be stored there. The
rule wins. Client *IDs* are public identifiers and live in YAML; the Gmail client secret comes
from `MAILLAUNCH_GMAIL_CLIENT_SECRET`. (Google issues a "secret" for installed apps that
isn't really secret — it ships inside the binary — but the config file is the thing that gets
committed by accident, so it stays out.)

**SQLite over a JSON file.** Not for query power — for durability. A JSON file has to be
rewritten whole, and a crash mid-write can truncate the entire campaign's state. SQLite gives
a real transaction per send.

**PKCE loopback OAuth.** The flow both Google and Microsoft document for installed apps. We
bind `127.0.0.1:0`, so the OS picks a free port and nothing needs pre-registering.

**The retry function takes the send function as an argument.** That's what makes backoff
testable against a mock instead of against Gmail.

**Trade-offs I accepted.** The whole CSV is read into memory — fine for the thousands of rows
a cold-email list contains, wrong for millions. Sends are strictly sequential, which the
60–180s delay makes irrelevant. The send log is plain text rather than JSON lines, because
the brief's sample format is human-readable and `grep` is the likely consumer.

---

## Assumptions

`maillaunch.html` was not attached to the brief, so the send logic here was rebuilt from the
brief's descriptions and the documented API contracts. Where the brief was silent or
self-contradictory I picked the option below — all are one-line changes if you'd rather have
it the other way.

| Question | What I did |
|---|---|
| `client_secret` in config vs. "no secrets in config" | Secrets come from the environment; config holds client IDs only |
| Log timestamps: "ISO 8601" vs. the sample's `2025-05-13 14:22:01` | Strict ISO 8601 with UTC offset — a send log without a timezone is ambiguous |
| Daily limit: calendar day or rolling 24h? Per account or global? | Local calendar day, global across campaigns and providers |
| Do retries consume quota? | No — only a delivered email counts |
| Hitting the limit mid-campaign | Pause and stay resumable, don't abort |
| Scheduling | Not built. The brief lists no `schedule` command; backgrounding the CLI solves "don't keep the browser open" |
| When is `SKIPPED` used? | Missing/invalid address, duplicate row, or missing template value |
| Missing `{{variable}}` | Skip the recipient rather than send a half-personalised email |
| Two columns match "mail" | Exact `email` wins, then `email`/`e-mail`, then `mail`; `--email-column` overrides. No match is a hard error that lists the columns |

---

## What I'd improve with more time

- **HTML email and attachments.** `build_mime` is plain-text only; multipart/alternative is a
  small change but untested, so I left it out rather than ship it half-done.
- **Per-recipient rate limiting beyond the daily cap** — Gmail also enforces per-minute quotas,
  and the current 429 handling reacts rather than anticipates.
- **`maillaunch log --campaign <id>`.** Logs are per-day files, so following a campaign that
  spans a limit pause means reading two files.
- **Token encryption at rest even in the keychain path**, for defence in depth on a shared
  machine.
- **A `--resume-failed` flag.** Right now a `FAILED` recipient is terminal; re-attempting them
  means a fresh campaign with a filtered CSV.
