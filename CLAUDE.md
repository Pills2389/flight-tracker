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

## fli source — read it locally instead of guessing

This machine has a local clone of **fli** (the Google Flights API library this
project depends on) at:

```
c:\Users\Cristi\Desktop\Repos\fli
```

When you need to know exactly how `SearchFlights`, `FlightSearchFilters`,
`FlightSegment`, `Airport`/`Airline`/`Alliance` enums, retry/backoff behaviour,
or any other fli internal works — **read the source there directly** (e.g.
`fli/search/flights.py`, `fli/models/google_flights/base.py`,
`fli/search/client.py`) rather than guessing from the installed package or
searching the web. It's faster, free of token waste, and always matches the
exact behaviour you'll see at runtime. Useful starting points:
- `fli/search/flights.py` — `SearchFlights.search()`, `_fetch_flights()`, `_expand_multi_leg()`
- `fli/models/google_flights/base.py` — `FlightSearchFilters`, `FlightSegment` (note `departure_airport`/`arrival_airport` are `list[list[Airport | int]]` — multiple airports per segment are natively supported, which is how multi-airport-city routes work, see below)
- `fli/search/client.py` — HTTP client's own `tenacity` retry/backoff
- `fli/core/airports.py` — `CITY_AIRPORTS` city→IATA mapping and `search_airports()`

---

## Architecture — data flow

```
config.json
    │
    ▼
load_config()
    │
    ▼
SearchFlights()  ←─── fli Python API (pip install flights)
    │
    ▼
search_route()
    │
    ├── _sample_dates()       Sample departure dates (or all matching weekdays)
    ├── _return_dates()       Generate return dates (respects return_days + max_return_date)
    ├── _build_filters()      Build typed FlightSearchFilters (Airport/Airline/MaxStops/...)
    ├── _run_search()         search.search(filters, top_n=..., currency=, ...), save debug dump if --debug
    └── _parse_pair()         (outbound, return) FlightResult tuple → normalized flight dict
         ├── outbound.legs    → outbound_legs (for dashboard tree)
         └── return.legs      → return_legs
    │
    ├── Duration filter       max_outbound_duration_hours / max_return_duration_hours
    ├── Self-transfer filter  exclude_self_transfer
    ├── Preferred airline     preferred_airlines / preferred_alliances + preferred_airline_mode
    └── Departure window      hard (TimeRestrictions on the outbound segment) or soft (flag only)
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
    "email":    { "enabled", "smtp_server", "smtp_port", "username", "password", "from_address", "to_address", "receive_error_report" },
    "whatsapp": { "enabled", "recipients": [{ "name", "phone", "api_key", "receive_error_report" }] },
    "ntfy":     { "enabled", "topic", "server", "receive_error_report" }
  },
  "_receive_error_report_note": "Opt-in flag for the batched end-of-run 🔴 error report (see 'Error reports' below) — same property name on all three channels (email/ntfy are single-recipient today, whatsapp is a per-recipient list) so the shape stays consistent if email/ntfy ever grow multi-recipient too. Defaults to false everywhere; independent of the per-route notification routing.",
  "route_defaults": "Optional sibling of 'routes' — see 'route_defaults (shared route settings)' below. Holds every field shown in the route schema except identity/endpoints/dates/max_price_alert; a route only needs to repeat a field here if its value should differ.",
  "routes": [{
    "id":                         "unique-id",
    "label":                      "Human readable name",
    "origin":                     "OTP",
    "destination":                "AKL",
    "_or_multi_city":             "Replace origin/destination with \"trip_type\": \"multi_city\" + \"legs\": [{origin,destination},{origin,destination}] for an open-jaw route — see 'Multi-city (open-jaw) routes' below",

    "date_from":                  "2027-02-01",
    "date_to":                    "2027-03-31",
    "max_return_date":            null,
    "target_nights":              20,
    "flexibility_days":           2,
    "daily_samples":              8,
    "top_n":                      20,
    "departure_days":             ["wednesday","thursday","friday"],
    "return_days":                ["sunday","monday"],
    "passengers":                 1,
    "currency":                   "EUR",
    "search_country":             "RO",
    "search_language":            "en",
    "preferred_airlines":         ["TK","QR"],
    "preferred_alliances":        ["STAR_ALLIANCE"],
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

## `route_defaults` (shared route settings)

`config.json` may carry a top-level `route_defaults` object — every entry in
`routes[]` is merged on top of it via `_merge_route_defaults()` in
`load_config()` before the rest of the code ever sees it. The route's own
values always win; **dicts are merged key-by-key (recursively)**, not replaced
wholesale, so a route can override e.g. just
`notifications.channels.whatsapp` while still inheriting `email`/`ntfy` from
the defaults — lists and scalars are replaced outright when the route
specifies them.

In practice this means a route block only needs:
- **Identity**: `id`, `label`, `enabled`
- **Endpoints**: `origin`/`destination` (or `trip_type`/`legs`)
- **Dates**: `date_from`, `date_to`, `max_return_date`
- **Price**: `max_price_alert`
- …plus *only* whichever search/filter/notification fields it wants to differ
  on — everything else (search behaviour, filters, `departure_window`,
  `notifications`, …) is inherited from `route_defaults`.

This is what keeps `config.json` from ballooning as routes are added — see
`config.json`'s `route_defaults` block for the actual shared values in use,
and routes like `otp-icn-2026` (overrides `preferred_airlines`) or
`otp-auk-2027` (overrides most filters for a long-haul profile) for examples
of partial overrides. `config.example.json` documents the same mechanism via
`_route_defaults_note` (its 3 example routes spell every field out in full for
teaching purposes instead of relying on it).

Entirely optional and backward compatible — if `route_defaults` is absent
(e.g. an older `FLIGHT_CONFIG` secret), the merge is a no-op and routes behave
exactly as if every field were specified inline.

---

## Notification logic

| Situation | What fires |
|---|---|
| Price ≤ `max_price_alert` | 🚨 Price alert (max once/day) |
| Alert sent today | Daily digest skipped (no duplicates) |
| `daily.enabled` + no threshold | Daily digest always fires |
| `daily.enabled` + threshold set | Daily only fires when price ≤ threshold |
| Configured weekday | Weekly summary (max once/week) — also doubles as a job heartbeat, see below |
| One or more routes throw an error this run | 🔴 Error report (batched, end of run) — see "Error reports" below |

### Weekly summary doubles as a heartbeat + API health check

`should_weekly()` fires on the configured weekday regardless of whether the
route found flights, so a heartbeat ("📭 no matches, job ran fine") still goes
out and silence ≠ "the job stopped running". It also carries a 7-day **raw API
health** line (`pairs_with_results/pairs_searched`, `avg raw flights/search`)
counted *before* client-side filtering — a drop toward zero means the API has
gone quiet, not that your filters got stricter. Full mechanics (which
functions persist/render this) → [IMPLEMENTATION_NOTES.md § Notification system internals](IMPLEMENTATION_NOTES.md#notification-system-internals).

### Per-trigger recipient routing (`weekly_summary.channels` override)

A route's `notifications.weekly_summary` block may carry its own `channels`
override used *only* for the weekly digest (e.g. send daily alerts to everyone
but keep the weekly summary to a smaller WhatsApp list), falling back to the
route's default `notifications.channels` otherwise. Resolved via
`_route_channels(route_notif, trigger)` in `dispatch()`. Details and a fixed
latent `ntfy_topic` bug → [IMPLEMENTATION_NOTES.md § Notification system internals](IMPLEMENTATION_NOTES.md#notification-system-internals).

### Error reports (separate opt-in recipient list)

Per-route exceptions are batched into one end-of-run 🔴 report
(`dispatch_error_report()`); recipients are configured **globally** via
`receive_error_report`, independent of the per-route `channels` routing used
for digests. Full flow (including the outer whole-job-crash handler) →
[IMPLEMENTATION_NOTES.md § Notification system internals](IMPLEMENTATION_NOTES.md#notification-system-internals).

---

## Departure day / return day logic

- `departure_days` set → ignores `daily_samples`, checks **every matching weekday** in window
- `departure_days` empty → samples `daily_samples` evenly across window
- `return_days` set → only generates return dates landing on those weekdays within flexibility range
- Falls back to exact target date if no return day matches (never skips a departure silently)
- `max_return_date` → hard cap, return dates beyond it are dropped

---

## Preferred airline / alliance modes

`preferred_airline_mode` controls both `preferred_airlines` (IATA codes, e.g.
`["TK", "QR"]`) and `preferred_alliances` (`["ONEWORLD", "SKYTEAM",
"STAR_ALLIANCE"]`):

| Mode | Behaviour |
|---|---|
| `off` | No preference, no flagging |
| `soft` | All flights shown, airline matches get 🏷️ flag (alliances **not** flaggable — see below) |
| `hard` | Only flights matching a preferred airline and/or alliance; passes `airlines=[...]` / `alliances=[...]` to `FlightSearchFilters` |

Supports multiple airlines/alliances — any leg matching any of them = match
(combined as one include list server-side, so it's an OR across both).
Old single string format `"preferred_airline": "TK"` still works (backward compatible).

**Why alliances can't be flagged in `soft` mode:** fli exposes `Alliance` purely
as a server-side filter token (`ONEWORLD`/`SKYTEAM`/`STAR_ALLIANCE`) — it has no
airline→alliance membership data we could use to test a parsed flight's airline
against a preferred alliance client-side. So `preferred_alliances` only takes
effect in `hard` mode, where fli does the matching itself before returning
results. In `hard` mode with both airlines and alliances configured, the
redundant client-side re-check (`search_route()`) is skipped entirely and we
trust fli's combined query — see `_get_preferred_alliances`/`_alliance_enums`
in `flight_tracker.py`.

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

# Debug mode — saves raw FlightResult/FlightLeg dumps to debug/ folder, shows filter breakdown
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

Currently runs once daily at 09:00 UTC = 12:00 Romania time (EEST/UTC+3 in
summer, 11:00 EET/UTC+2 in winter — cron is UTC and doesn't follow DST).

Config is stored as GitHub Secret `FLIGHT_CONFIG` (entire config.json contents).
After each run, the workflow commits `price_history.json` and `docs/` back to the repo.

---

## Key implementation notes

Deep-dive explanations of *why* the code works the way it does live in
[IMPLEMENTATION_NOTES.md](IMPLEMENTATION_NOTES.md) — read it before touching
any of these areas, since the reasoning isn't obvious from the code alone:

- **fli search shape & `top_n`** — round-trip vs multi-city result shapes,
  why `top_n` defaults to 20 (fli's own default of 5 silently drops airlines),
  and how `_run_search()` drives `_fetch_flights()`/`_expand_multi_leg()`
  directly (`_prefilter_outbound()`/`_rank_outbound()`) to spend those
  expansion slots on flights the route actually wants.
- **Empty-result retries** — Google sporadically returns an empty payload for
  date pairs that genuinely have flights; `_run_search()` retries up to
  `search_retries` (default 3) before giving up.
- **Multi-airport cities** (`origin`/`destination` as a list, e.g.
  `["NRT","HND"]`) and **multi-city/open-jaw routes**
  (`"trip_type": "multi_city"` + 2-entry `"legs"`) — config shape, how prices
  are read off the correct segment per trip type, and label rendering.
- **`search_country`** — without `"RO"`, Google omits airlines popular from
  Romania (e.g. Turkish Airlines).
- **Duration filtering is client-side** — `max_outbound/return_duration_hours`
  aren't passed to fli (it only supports one combined `max_duration`); they're
  applied after the API call in `search_route()`.
- **`price_history.json` size** — up to 30 flights/route/day, 180 days, full
  leg data for the dashboard tree.
- **Windows encoding** — all file opens use `encoding="utf-8"` explicitly
  (route labels contain `→`, U+2192, which breaks cp1252).

---

## Common tasks

**Add a new route:**
Copy an existing route block in `config.json` (or start from scratch — see
[`route_defaults`](#route_defaults-shared-route-settings) above for what a
route can omit), set `id`, `label`, endpoints, dates, and `max_price_alert`,
and add only the search/filter/notification fields that should differ from
`route_defaults`. Update the `FLIGHT_CONFIG` GitHub Secret.

**Add a new open-jaw / multi-city route:**
Copy the `otp-hkg-nrt-2027` example block in `config.example.json` — set
`"trip_type": "multi_city"`, a 2-entry `"legs"` array (each `{origin,
destination}`), and reuse `date_from`/`date_to`/`target_nights`/
`flexibility_days`/`return_days` exactly as you would for a round-trip (they
now describe leg 1's window and the gap to leg 2). See
[Multi-city (open-jaw) routes](#multi-city-open-jaw-routes-trip_type-multi_city--legs).

**Change notification threshold:**
Update `max_price_alert` in the relevant route. Update GitHub Secret.

**Change run schedule:**
Edit the `cron:` line in `.github/workflows/flight_check.yml`. Remember cron is
UTC and ignores DST — e.g. `'0 9 * * *'` is 09:00 UTC = 12:00 Romania time in
summer (EEST/UTC+3) but 11:00 in winter (EET/UTC+2).

**Add a new filter:**
1. Add field to `config.example.json` with a `_note` comment
2. If it maps to a `FlightSearchFilters` field (stops, airlines, layovers, time
   restrictions, bags, ...), read it in `_build_filters()` and pass it through typed
   (e.g. `MaxStops`, `LayoverRestrictions`, `TimeRestrictions`, `BagsFilter`)
3. If it can't be expressed server-side (e.g. separate outbound/return duration caps),
   read it in `search_route()` and filter client-side after `_run_search()`
4. Add to the `[DEBUG] Filtered out:` log line if it's a client-side filter

**Increase airline coverage (e.g. catch TK in results):**
Raise `"top_n"` in the route config — it controls how many cheapest outbound options
get expanded into return-trip searches (fli default is 5; we default to 20). See
[top_n controls how many outbound options get expanded into return searches](#key-implementation-notes).

**Debug missing airline:**
```bash
python flight_tracker.py --debug --route ROUTE_ID
```
Check `debug/` folder JSON files (now raw `FlightResult`/`FlightLeg` Pydantic dumps,
one `[outbound, return]` pair per entry). Look at `[DEBUG] Airlines in raw results:`
log lines. If airline missing from raw → Google not returning it for the searched
outbound candidates (try raising `top_n` or adding `search_country`). If in raw but
not in final → check duration/self-transfer/airline filters.
