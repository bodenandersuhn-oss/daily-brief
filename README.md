# Daily brief

A weekday market / AI news brief. Runs on GitHub Actions at 6:30am US Central,
publishes an HTML page to GitHub Pages, and sends a push notification to your
phone via [ntfy.sh](https://ntfy.sh). Nothing runs on your laptop.

**Pipeline:** `fetch` → `dedupe` → `summarize` → `publish`

Four sections, fixed order — US economy, International economies,
AI infrastructure & energy, AI news — so five dailies roll into one weekly
digest without reformatting.

---

## Setup

### 1. Push this to GitHub

```bash
git remote add origin git@github.com:YOUR_USER/daily-brief.git
git push -u origin main
```

### 2. Turn on GitHub Pages

Repo **Settings → Pages** → Source: *Deploy from a branch* → branch `main`,
folder **`/docs`**. Your brief will be at
`https://YOUR_USER.github.io/daily-brief/`.

### 3. Allow the workflow to commit

Repo **Settings → Actions → General → Workflow permissions** → select
**Read and write permissions**. The job commits each rendered page and the
dedupe database back to the repo.

### 4. Add the repo secrets

**Settings → Secrets and variables → Actions → New repository secret.**

| Secret | Required | What it is |
|---|---|---|
| `ANTHROPIC_API_KEY` | yes | Claude API key from [console.anthropic.com](https://console.anthropic.com). Without it the brief still publishes, as raw deduplicated headlines with no summaries. |
| `NTFY_TOPIC` | yes | Your ntfy topic name (see §5). Without it the page publishes but no push is sent. |
| `GMAIL_CLIENT_ID` | for WSJ email | OAuth client ID (see §6). |
| `GMAIL_CLIENT_SECRET` | for WSJ email | OAuth client secret (see §6). |
| `GMAIL_REFRESH_TOKEN` | for WSJ email | Long-lived refresh token (see §6). |

If the Gmail secrets are absent the newsletter pull is skipped with a warning
and the RSS half of the brief runs normally.

There is no secret for the Pages URL — it is derived from
`GITHUB_REPOSITORY`. Set the optional `PAGES_URL` environment variable only if
you serve the page from a custom domain.

### 5. ntfy push notifications

1. Install the **ntfy** app (iOS / Android).
2. Pick a topic name that nobody will guess — anyone who knows the topic can
   read your notifications. Something like `brief-a7f3k9x2qp`.
3. Subscribe to that topic in the app.
4. Save the same string as the `NTFY_TOPIC` repo secret.

The push arrives as: title `Morning Brief — Sep 8`, body being the top item
from each of the four sections, one line each. Tapping it opens that day's
full page.

### 6. Gmail: getting a refresh token

The brief reads your WSJ newsletter emails read-only. **Subscribe yourself**
to WSJ *Markets A.M.*, *Real Time Economics*, and *WSJ Tech* first — this
project does not sign in to WSJ and never touches the paywall.

**a. Create the OAuth client**

1. Go to [console.cloud.google.com](https://console.cloud.google.com) and
   create a project (any name).
2. **APIs & Services → Library** → search "Gmail API" → **Enable**.
3. **APIs & Services → OAuth consent screen** → User type **External** →
   fill in app name and your email → Save.
   - Under **Audience**, add your own Gmail address as a **Test user**.
     Without this the token request is rejected.
   - Leaving the app in *Testing* is fine. Note that refresh tokens for
     apps in Testing expire after 7 days — click **Publish app** on the
     consent screen to get a token that does not expire.
4. **APIs & Services → Credentials → Create Credentials → OAuth client ID** →
   Application type **Desktop app** → Create.
5. Copy the **Client ID** and **Client secret**.

**b. Generate the refresh token**

Run this once on your own machine. It opens a browser window, you approve
access, and it prints the token. Nothing is written to disk.

```bash
pip install google-auth-oauthlib
```

```bash
python3 - <<'EOF'
from google_auth_oauthlib.flow import InstalledAppFlow

CLIENT_ID = "PASTE_YOUR_CLIENT_ID"
CLIENT_SECRET = "PASTE_YOUR_CLIENT_SECRET"

flow = InstalledAppFlow.from_client_config(
    {"installed": {
        "client_id": CLIENT_ID,
        "client_secret": CLIENT_SECRET,
        "auth_uri": "https://accounts.google.com/o/oauth2/auth",
        "token_uri": "https://oauth2.googleapis.com/token",
        "redirect_uris": ["http://localhost"],
    }},
    scopes=["https://www.googleapis.com/auth/gmail.readonly"],
)
creds = flow.run_local_server(port=0, prompt="consent", access_type="offline")
print("\nGMAIL_REFRESH_TOKEN:\n" + creds.refresh_token)
EOF
```

`prompt="consent"` matters — without it Google returns no refresh token on
repeat authorizations.

**c.** Save the three values as the `GMAIL_*` repo secrets. Do not commit them.

### 7. Test it

**Actions → Daily brief → Run workflow.** Manual runs bypass the time guard,
so you do not have to wait until 6:30am.

---

## Scheduling and DST

GitHub Actions cron is UTC only, and US Central shifts with daylight saving.
The workflow registers **both** `30 11 * * 1-5` and `30 12 * * 1-5`, so one of
them is always 6:30am Central. The first step checks the actual
`America/Chicago` hour and exits cleanly when it is not 6, so the other
schedule is a no-op rather than a duplicate brief.

`workflow_dispatch` runs skip the guard entirely.

---

## Editing sources

Everything lives in `feeds.yaml`; no code changes needed. Add a feed by adding
a line to the relevant section:

```yaml
- {outlet: "Outlet name", name: "Which feed", url: "https://example.com/rss"}
```

`settings` at the top of the file controls the lookback window, per-section
item cap, dedupe strictness, and the User-Agent. Some government feeds (BLS
notably) reject generic browser user-agents and require a descriptive one, so
keep that field descriptive.

Feeds that break are logged and skipped — one dead feed never takes the run
down. Watch the Actions log for repeated `SKIP` lines.

### Sources that were requested but do not exist

Every URL in `feeds.yaml` was verified to return valid XML. These were dropped
because they 404, are blocked, or publish no feed at all:

| Requested | Why dropped | Substituted with |
|---|---|---|
| Reuters US markets / world / business RSS | Reuters discontinued public RSS; `feeds.reuters.com` no longer resolves | CNBC Economy, MarketWatch, BBC, Guardian |
| AP business RSS | 404 / 401 — AP retired public RSS | NPR Business, CNBC |
| IMF news | 403 from every path and user-agent | BBC/Guardian world, EU Commission |
| OECD newsroom | 404, and the `.xml` path returns non-XML | — |
| FERC news releases | No public RSS feed; every documented path 404s | Utility Dive covers FERC actions |
| EIA weekly electricity update | No such feed exists | EIA *Today in Energy* + EIA press releases |
| Anthropic news | Publishes no RSS feed | — |
| Meta AI blog | Publishes no RSS feed | Meta Newsroom |
| Microsoft / Alphabet / Amazon / Meta IR press feeds | All 404 | Their newsroom and blog feeds |
| US Treasury press | Connection timeout | — |

Nvidia's investor-relations feed **does** exist and is included.

---

## How dedupe works

`state/seen.db` is a SQLite store of every URL and normalized title the brief
has already seen. Two passes:

1. **Within a run** — identical canonical URLs merge, then titles are compared.
   Named actors gate the comparison, so "Microsoft opens datacenter in Ohio"
   and "Amazon opens datacenter in Ohio" stay separate while "Fed holds rates
   steady" and "Federal Reserve holds interest rates steady" collapse into one.
   Extra outlets are listed on the item as *also: …*.
2. **Across days** — anything matching the last `repeat_window_days` (default
   7) is dropped.

The database is **committed to the repo on purpose**. Actions runners are
ephemeral; without it in git, every brief would repeat yesterday's stories.

Everything that survives dedupe is recorded, not just what the model selected —
an unselected item would fall out of the 24-hour window tomorrow anyway.

To start over: `rm state/seen.db`, then commit.

---

## Accuracy rules

These are enforced in code, not just requested in the prompt. Any summary that
violates one is discarded and the item renders as headline-only:

- The model never emits a URL or an outlet name. It returns a reference number
  and links are joined back from what was actually fetched, so a fabricated
  link is impossible.
- Two sentences maximum.
- No number, figure, or date may appear unless it is present in the fetched
  source text for that item.
- No verbatim runs from the source — an 8-word overlap is treated as pasted
  text and rejected.
- An item with no body text is tagged `headline only` and gets no summary at
  all, rather than prose inferred from its headline.
- Sections cap at 5 items. An empty section prints *Nothing significant*
  rather than filler.

WSJ and MarketWatch links return HTTP 401 to anonymous requests — that is the
paywall, not a broken link. They open normally in a browser where you are
signed in.

---

## Running locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
```

```bash
.venv/bin/python -m brief.run --dry-run
```

| Flag | Effect |
|---|---|
| `--dry-run` | Fetch and assemble, print to stdout. No page, no commit, no push. |
| `--no-summarize` | Skip the Claude API call; show raw deduplicated headlines. |
| `--no-record` | Do not write to the seen-database, so a run repeats identically. |
| `-v` | Debug logging. |

Secrets are read from the environment only. `.env` is gitignored; no
credential is ever written to a committed file.
