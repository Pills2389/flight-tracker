# Flight Price Tracker — Project Context for Claude

## What this project does

Tracks round-trip flight prices across multiple routes using **fli** (reverse-engineered
Google Flights API — no API key needed). Runs daily on GitHub Actions, stores price
history in `price_history.json` (committed to the repo), generates a live dashboard
on GitHub Pages, and sends notifications via Email, WhatsApp (CallMeBot), and ntfy.sh.

---

## Key files

| File | Purpose |
|---|---|
| `flight_tracker.py` | Single entry point — all logic lives here |
| `config.json` | User config (gitignored — never commit) |
| `config.example.json` | Documented template for config.json |
| `price_history.json` | Price history committed to repo (auto-updated by Actions) |
| `test.py` | Test suite — run before pushing changes |
| `docs/index.html` | Auto-generated dashboard (GitHub Pages) |
| `.github/workflows/flight_check.yml` | GitHub Actions schedule |
| `debug/` | Raw API dumps when running with --debug (gitignored) |

---

## Architecture — data flow

```
config.json
    │
    ▼
load_config()
    │
    ▼
find_fli()  ←─── fli CLI (pip install flights click)
    │
    ▼
search_route()
    │
    ├── _sample_dates()       Sample departure dates (or all matching weekdays)
    ├── _return_dates()       Generate return dates (respects return_days + max_return_date)
    ├── _build_cmd()          Build fli CLI command with all filters
    ├── _run_fli()            Execute fli, save debug dump if --debug
    └── _parse_one()          Parse fli JSON → normalized flight dict
         ├── outbound.legs    → outbound_legs (for dashboard tree)
         └── return.legs      → return_legs
    │
    ├── Duration filter       max_outbound_duration_hours / max_return_duration_hours
    ├── Self-transfer filter  exclude_self_transfer
    ├── Preferred airline     preferred_airlines + preferred_airline_mode
    └── Departure window      hard (--time to fli) or soft (flag only)
    │
    ▼
process_route()
    │
    ├── add_entry()           Save today's best to price_history.json
    ├── analyze_trend()       7d/14d moving average, buy/wait signal
    ├── format_message()      Plain text + HTML for notifications
    └── dispatch()            Email + WhatsApp + ntfy
    │
    ▼
generate_dashboard()          Writes docs/index.html
    ├── _group_flights()      Group by airline, top N per group
    ├── _render_airline_group()
    ├── _render_option()      Collapsible parent row
    └── _render_direction()   Collapsible outbound/return with legs
```

---

## Config schema (complete)

```json
{
  "notifications": {
    "email":    { "enabled", "smtp_server", "smtp_port", "username", "password", "from_address", "to_address" },
    "whatsapp": { "enabled", "phone", "api_key" },
    "ntfy":     { "enabled", "topic", "server" }
  },
  "routes": [{
    "id":                         "unique-id",
    "label":                      "Human readable name",
    "origin":                     "OTP",
    "destination":                "AKL",
    "date_from":                  "2027-02-01",
    "date_to":                    "2027-03-31",
    "max_return_date":            null,
    "target_nights":              20,
    "flexibility_days":           2,
    "daily_samples":              8,
    "departure_days":             ["wednesday","thursday","friday"],
    "return_days":                ["sunday","monday"],
    "passengers":                 1,
    "currency":                   "EUR",
    "search_country":             "RO",
    "search_language":            "en",
    "preferred_airlines":         ["TK","QR"],
    "preferred_airline_mode":     "soft",
    "max_stopovers":              2,
    "max_layover_hours":          8,
    "max_outbound_duration_hours": 22,
    "max_return_duration_hours":  22,
    "exclude_self_transfer":      true,
    "bags":                       0,
    "max_per_airline":            3,
    "departure_window": { "enabled": true, "from": "20:00", "to": "23:59", "mode": "soft" },
    "max_price_alert":            1500,
    "notifications": {
      "daily":          { "enabled": true },
      "price_alert":    { "enabled": true },
      "weekly_summary": { "enabled": true, "day": "sunday" },
      "channels": {
        "email": true, "whatsapp": true, "ntfy": true,
        "ntfy_topic": "otp-akl-alerts"
      }
    }
  }]
}
```

---

## Notification logic

| Situation | What fires |
|---|---|
| Price ≤ `max_price_alert` | 🚨 Price alert (max once/day) |
| Alert sent today | Daily digest skipped (no duplicates) |
| `daily.enabled` + no threshold | Daily digest always fires |
| `daily.enabled` + threshold set | Daily only fires when price ≤ threshold |
| Configured weekday | Weekly summary (max once/week) |

---

## Departure day / return day logic

- `departure_days` set → ignores `daily_samples`, checks **every matching weekday** in window
- `departure_days` empty → samples `daily_samples` evenly across window
- `return_days` set → only generates return dates landing on those weekdays within flexibility range
- Falls back to exact target date if no return day matches (never skips a departure silently)
- `max_return_date` → hard cap, return dates beyond it are dropped

---

## Preferred airline modes

| Mode | Behaviour |
|---|---|
| `off` | No preference, no flagging |
| `soft` | All flights shown, matching ones get 🏷️ flag |
| `hard` | Only flights with at least one leg on preferred airline; passes `--all` to fli |

Supports multiple airlines: `["TK", "QR"]` — any leg matching any airline = match.
Old single string format `"preferred_airline": "TK"` still works (backward compatible).

---

## Dashboard structure

- Price summary card (best price, all-time best, trend signal)
- 30-day Chart.js price chart with 14d moving average line
- Options grouped by airline (`_group_flights` → `_render_airline_group`)
  - Each group: collapsible, sorted by cheapest, top `max_per_airline` (default 3)
  - Each option: collapsible parent row → outbound `<details>` + return `<details>`
  - Each direction: legs with dep/arr times, airline, aircraft, layovers (🌙 overnight)

---

## Running locally

```bash
# Normal run
python flight_tracker.py

# Debug mode — saves raw fli JSON to debug/ folder, shows filter breakdown
python flight_tracker.py --debug

# Debug one route only
python flight_tracker.py --debug --route otp-akl-2027

# Test suite (fast — no live search)
python test.py --no-live

# Test suite + live flight search
python test.py

# Test suite + send real notifications
python test.py --notify
```

---

## GitHub Actions

Workflow: `.github/workflows/flight_check.yml`

Currently runs every 2 hours, 07:00–17:00 UTC (10:00–20:00 Romania EEST).
After initial validation period, change to once daily: `cron: '0 7 * * *'`

Config is stored as GitHub Secret `FLIGHT_CONFIG` (entire config.json contents).
After each run, the workflow commits `price_history.json` and `docs/` back to the repo.

---

## Key implementation notes

### fli JSON structure (round-trip)
```json
{
  "price": 789.0,
  "duration": 2405,
  "stops": 2,
  "outbound": { "duration": 1195, "legs": [...], "layovers": [...], "self_transfer": false },
  "return":   { "duration": 1210, "legs": [...], "layovers": [...] }
}
```
One-way flights have `legs` and `layovers` at top level instead of nested under `outbound`.

### Why search_country matters
Without `"search_country": "RO"`, Google returns generic results that may omit
airlines popular from Romania (e.g. Turkish Airlines). Setting it to `"RO"` mimics
searching from Romania and returns the same results as a manual search.

### Duration filtering is client-side
`max_outbound_duration_hours` and `max_return_duration_hours` are NOT passed to fli.
They filter results after the API call. fli does not support duration filtering natively.

### price_history.json size
Stores up to 30 flights per route per day, 180 days history.
Each flight includes full leg data for the dashboard tree view.
The `raw` field from fli is stripped before storage to keep file size manageable.

### Windows encoding
All file opens use `encoding="utf-8"` explicitly. Route labels contain `→` (U+2192)
which breaks Windows default cp1252 encoding. Log messages with emoji use the
UTF-8 stream handler workaround in the logging setup.

---

## Common tasks

**Add a new route:**
Copy an existing route block in `config.json`, change `id`, `origin`, `destination`,
dates, and notification settings. Update the `FLIGHT_CONFIG` GitHub Secret.

**Change notification threshold:**
Update `max_price_alert` in the relevant route. Update GitHub Secret.

**Switch from 2-hourly to daily:**
In `.github/workflows/flight_check.yml`, replace the 6-entry cron with:
`- cron: '0 7 * * *'`

**Add a new filter:**
1. Add field to `config.example.json` with a `_note` comment
2. Read it in `search_route()` where other filters are applied
3. Add to the `[DEBUG] Filtered out:` log line

**Debug missing airline:**
```bash
python flight_tracker.py --debug --route ROUTE_ID
```
Check `debug/` folder JSON files. Look at `[DEBUG] Airlines in raw results:` log lines.
If airline missing from raw → Google not returning it (try adding `search_country`).
If in raw but not in final → check duration/self-transfer/airline filters.
