# Gym Sync

Import Jefit + Whoop into one local database and generate combined training insights.

Whoop can sync live via the official API (OAuth), or from zip/CSV exports. Journal answers are export-only.

## Setup

1. Install Python 3.10+
2. Drop exports into (optional if using Whoop API + Jefit auto-sync):
   - `imports/jefit/` → Jefit CSV export
   - `imports/whoop/` → Whoop zip export

## Commands

```bash
# Import latest exports
python sync.py import-all

# Or import individually
python sync.py import-jefit "C:\path\to\jefit_export.csv"
python sync.py import-whoop "C:\path\to\my_whoop_data.zip"

# Auto-sync Jefit (picks newest CSV from Downloads / imports/jefit)
python sync.py jefit-sync --csv-only

# Check if public scrape is available (needs privacy = Everyone)
python sync.py jefit-status

# Whoop API (after developer app + credentials — see below)
python sync.py whoop-auth --client-id YOUR_ID --client-secret YOUR_SECRET --open
python sync.py whoop-sync
python sync.py whoop-status

# Generate report (prints to terminal)
python sync.py report

# Save report files
python sync.py report --save --days 14

# Start web dashboard
python sync.py dashboard
```

Open **http://127.0.0.1:5000** in your browser.

## Whoop API

Configured in `config.json` → `integrations.whoop`. Secrets stay out of git:

| File / env | Purpose |
|------------|---------|
| `data/whoop_secrets.json` or `WHOOP_CLIENT_ID` / `WHOOP_CLIENT_SECRET` | Developer app credentials |
| `data/whoop_tokens.json` | OAuth access + refresh tokens (created after auth) |

### One-time developer app

1. Sign in at [developer.whoop.com](https://developer.whoop.com/) and create an App
2. Scopes: `read:recovery`, `read:cycles`, `read:sleep`, `read:workout`, `offline`
3. Paste these GitHub Pages URLs (served from this `fitcoach` repo’s `docs/` folder):

| Field | Exact value to paste |
|-------|----------------------|
| **Privacy Policy URL** | `https://roshancodes24.github.io/fitcoach/privacy.html` |
| **Redirect URL** | `https://roshancodes24.github.io/fitcoach/whoop-callback.html` |

4. Copy Client ID and Client Secret

WHOOP expects an `https://` redirect (not plain `http://127.0.0.1`). After consent, `whoop-callback.html` hands the auth code to your local dashboard at `http://127.0.0.1:5000/api/whoop/callback` (or you paste the code into `whoop-auth --code`).

**GitHub Pages:** enable Pages for `roshancodes24/fitcoach` → Source: Deploy from branch → `master` / `/docs`. The repo is currently private; Pages must be publicly reachable for WHOOP to load the privacy URL (make the repo public, or use a GitHub plan that publishes private Pages). After the first push of `docs/`, wait a minute and open the privacy URL to confirm it loads.

### Connect and sync

```bash
# Save credentials and open the authorize URL
python sync.py whoop-auth --client-id YOUR_ID --client-secret YOUR_SECRET --open

# Or start the dashboard and click Connect on the Whoop card
python sync.py dashboard

# After authorizing, pull recent recovery / sleep / strain / workouts
python sync.py whoop-sync --api-only

# Modes
python sync.py whoop-sync --export-only   # newest zip/CSV in imports/whoop
python sync.py whoop-sync --auto          # API first, export fallback
```

If the browser callback fails, copy the redirected URL (or `code=…`) and run:

```bash
python sync.py whoop-auth --code "PASTE_CODE_OR_FULL_CALLBACK_URL"
```

**Note:** Whoop journal prompts (e.g. protein) are not in the API — keep uploading a zip export when you need those answers.

| Mode | Command / UI | What it does |
|------|----------------|--------------|
| **API** | `whoop-sync --api-only` or dashboard **Sync** | Live pull into `whoop_daily` / `whoop_workouts` |
| **Export** | `whoop-sync --export-only` or zip upload | Classic zip/CSV import (includes journal) |
| **Auto** | `whoop-sync --auto` | API first; falls back to newest export |

## Jefit automation

Configured in `config.json` → `integrations.jefit` (your user id is `10560896`).

| Mode | Command | What it does |
|------|---------|--------------|
| **CSV (recommended)** | `python sync.py jefit-sync --csv-only` | Finds newest matching export in Downloads / `imports/jefit`, imports it |
| **Scrape** | `python sync.py jefit-sync --scrape-only` | Pulls public web logs (requires Jefit privacy = **Everyone**) |
| **Auto** | `python sync.py jefit-sync --auto` | CSV first, then scrape if public |

**Today:** your Jefit logs are **private**, so scrape is blocked. Workflow that works now:

1. Export CSV from the Jefit app (save to Downloads)
2. Run `python sync.py jefit-sync --csv-only`
3. Or POST `/api/jefit/sync` with `{"mode":"csv"}` from the dashboard later

To enable scrape later: Jefit app → settings → privacy → **Everyone**, then `python sync.py jefit-status`.

Optional Windows Task Scheduler (after gym): run  
`python sync.py jefit-sync --csv-only` every evening.

## Upload from dashboard

1. **Jefit app** → Export data → upload the `.csv` file in the Jefit card
2. **Whoop** → click **Connect** (once), then **Sync** for live API data — or upload a `.zip` export
3. Dashboard refreshes automatically after each upload/sync

You can also drag and drop files onto each upload card.

## Dashboard features

- **Today** — planned session, Whoop recovery, sleep, training recommendation
- **This week** — Mon–Sun plan vs what you actually did
- **Daily log** — recovery + gym sessions (click a row for workout detail)
- **Insights** — auto-generated coaching flags
- **Import button** — re-sync files from `imports/` without using the terminal

## What it syncs

| Source | Data |
|--------|------|
| **Jefit** | Workout sessions, exercises, sets, volume, day names (e.g. Push A) |
| **Whoop (API)** | Recovery, HRV, RHR, sleep, strain, workouts |
| **Whoop (export)** | Same as API, plus journal answers |

## Insights generated

- Today's planned session vs Whoop recovery (green/yellow/red guidance)
- Gym + recovery daily log
- Post-gym next-day recovery pattern
- Missed sessions vs your `config.json` schedule
- Sleep and protein journal flags

## Re-export workflow

1. Export fresh data from Jefit app
2. Sync Whoop via API (`whoop-sync`) or export a zip
3. Copy Jefit files into `imports/` if needed
4. Run `python sync.py import-all` (or `jefit-sync` / `whoop-sync`)
5. Run `python sync.py report --save`
6. Share the report in chat for coaching feedback

Data stays local in `data/gym.db`.
