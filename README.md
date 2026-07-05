# Gym Sync

Import Jefit + Whoop exports into one local database and generate combined training insights.

## Setup

1. Install Python 3.10+
2. Drop exports into:
   - `imports/jefit/` → Jefit CSV export
   - `imports/whoop/` → Whoop zip export

## Commands

```bash
# Import latest exports
python sync.py import-all

# Or import individually
python sync.py import-jefit "C:\path\to\jefit_export.csv"
python sync.py import-whoop "C:\path\to\my_whoop_data.zip"

# Generate report (prints to terminal)
python sync.py report

# Save report files
python sync.py report --save --days 14

# Start web dashboard
pip install -r requirements.txt
python sync.py dashboard
```

Open **http://127.0.0.1:5000** in your browser.

## Upload from dashboard

1. **Jefit app** → Export data → upload the `.csv` file in the Jefit card
2. **Whoop app** → Export data → upload the `.zip` file in the Whoop card
3. Dashboard refreshes automatically after each upload

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
| **Whoop** | Recovery, HRV, RHR, sleep, strain, workouts, journal answers |

## Insights generated

- Today's planned session vs Whoop recovery (green/yellow/red guidance)
- Gym + recovery daily log
- Post-gym next-day recovery pattern
- Missed sessions vs your `config.json` schedule
- Sleep and protein journal flags

## Re-export workflow

1. Export fresh data from Jefit app
2. Export fresh data from Whoop app
3. Copy files into `imports/`
4. Run `python sync.py import-all`
5. Run `python sync.py report --save`
6. Share the report in chat for coaching feedback

Data stays local in `data/gym.db`.
