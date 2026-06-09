# Implementation Notes — Flight Price Tracker

Deep-dive explanations referenced from `CLAUDE.md`. Read this when you need the
*why* behind a specific piece of `flight_tracker.py` — `CLAUDE.md` keeps the
steering rules and architecture; this file holds the narrative detail.

---

## Notification system internals

### Weekly summary doubles as a heartbeat + API health check

`should_weekly()` no longer requires that the route found any flights —
it fires on the configured weekday regardless, so silence on a route
(zero matches for days/weeks) is distinguishable from "the job stopped
running". When no flights matched this week, `format_heartbeat_message()`
sends a short "📭 no matches, job ran fine" notice instead of the full
price report (`process_route()` branches on this *before* its early
`return` for empty results).

Either way, the weekly message includes a 7-day **raw API health** line —
`pairs_with_results / pairs_searched` and `avg raw flights/search`,
counted in `search_route()` *before* any client-side filtering
(duration/self-transfer/airline) and persisted per-route per-day via
`add_api_stats()` → `history[route_id]["api_health"]`
(`api_health_summary()` rolls the last 7 days into one figure,
`_api_health_line()` renders it). Because these counts are pre-filter,
a sudden drop toward zero means the *API* has gone quiet — not that
your filters got stricter — which is the signal you're watching for.

### Per-trigger recipient routing (`weekly_summary.channels` override)

`dispatch(content, cfg, route, trigger=...)` resolves which `channels`
config applies via `_route_channels(route_notif, trigger)`: a route's
`notifications.weekly_summary` block may carry its own `channels` override,
used *only* for the weekly digest, falling back to the route's default
`notifications.channels` for daily/price-alert dispatches and whenever no
override is set. This is how you send daily updates/alerts to everyone but
keep the weekly summary to a smaller list — e.g.
`weekly_summary.channels.whatsapp: ["cristian"]` while the route default is
`channels.whatsapp: true`. Same shape/rules as the route-level `channels`
(`true`/`false`/list of recipient names for whatsapp, `true`/`false` for
email/ntfy, optional `ntfy_topic`).

Refactoring this also fixed a latent bug: `send_ntfy()` previously read
`ntfy_topic` off the whole `notifications` dict instead of `notifications.
channels` (where it actually lives in the config schema), so a route's
custom ntfy topic was silently ignored and everything went to the global
topic. `_active_channels`/`_whatsapp_recipients`/`send_ntfy` now all take
the already-resolved `channels` dict directly.

### Error reports (separate opt-in recipient list)

Per-route exceptions are caught in `run()`'s loop (so one broken route
doesn't kill the rest) and collected into `route_errors`; at the end of
the run `dispatch_error_report()` sends one batched 🔴 summary — *if*
anyone has opted in. A second, outer try/except around all of `run()`
catches whole-job crashes (bad config, dashboard generation, etc.) and
sends a single-item report the same way before re-raising (so GitHub
Actions still marks the run failed and its own failure email remains the
backstop for crashes that happen before `cfg` even loads).

Error-report recipients are configured **globally**, independent of the
per-route `channels` routing used for digests — see `receive_error_report`
in the config schema (`CLAUDE.md`). `_error_report_targets()` resolves them.

---

## fli search internals

### fli Python API — round-trip search shape
We call `SearchFlights().search(filters, top_n=..., currency=, language=, country=)`
(`fli.search.SearchFlights`, not the CLI subprocess). For `TripType.ROUND_TRIP` it
returns `list[(outbound: FlightResult, return: FlightResult)] | None`. The combined
round-trip price/duration/stops live on (or are summed from) the outbound leg —
mirrored from fli's own CLI serialization (`outbound.price`, `outbound.duration +
return.duration`, `outbound.stops + return.stops`). `_parse_pair()` builds the
normalized flight dict directly from these typed `FlightResult`/`FlightLeg`/`Layover`
Pydantic models (real `datetime` objects — no ISO-string parsing needed).

### top_n controls how many outbound options get expanded into return searches
`search()` only fetches return-trip options for the cheapest `top_n` outbound
candidates (fli expands each in a parallel follow-up call, rate-limited to Google's
10 req/sec ceiling). The fli default is 5, which can silently miss airlines that
don't appear among the 5 cheapest outbound legs (this is why TK was sometimes
missing from results). Configurable per-route via `"top_n"` in config.json
(`route.get("top_n", 20)` in `search_route()`); higher values cost more requests
and a longer run, but find more carriers.

### Outbound candidate selection — we drive fli's expansion ourselves
`_run_search()` does **not** call fli's `SearchFlights.search()`. It calls
`search._fetch_flights()` and `search._expand_multi_leg()` directly — both
private fli methods (`fli/search/flights.py`) — so we can control *which*
outbound `FlightResult`s get one of `top_n`'s expensive `_expand_multi_leg`
slots, instead of fli always picking the cheapest `top_n` raw outbound options.
Two helpers run in between the two calls:

- `_prefilter_outbound()` drops outbound candidates that `search_route()`'s
  client-side filters would discard anyway — `max_outbound_duration_hours`
  (`FlightResult.duration`, safe for both trip types) and, **round-trip routes
  only**, `exclude_self_transfer` (`FlightResult.self_transfer` — for
  multi-city the combined-trip flag `_parse_pair` reads lives on the *return*
  leg, not the outbound, so pre-checking it on the raw outbound would use the
  wrong segment and risk dropping good itineraries).
- `_rank_outbound()` then reorders the survivors by **preference tier, then
  price** — matches on `preferred_airlines`/`preferred_alliances` (when
  `preferred_airline_mode` isn't `off`) and `departure_window` (when enabled)
  win a slot ahead of merely-cheaper flights that don't match. Best price isn't
  everything — an off-hours flight on a non-preferred carrier costs time even
  when it's a few EUR cheaper. Routes with neither preference configured collapse
  back to plain price-sort, so behaviour for them is unchanged from before.

Net effect: `top_n` expansion slots are spent on flights the route is actually
configured to want (and that won't be filtered out downstream), rather than on
the cheapest raw options regardless of fit — without firing any extra requests.
Because these are private fli methods, a future fli upgrade that renames or
reshapes `_fetch_flights`/`_expand_multi_leg` needs to be re-checked here.

### Empty results can be a transient Google glitch — retried automatically
Google's backend occasionally returns an empty payload for a date pair that
genuinely has matching flights — confirmed by hand: re-running the *exact
same* query (same filters, same hard `airlines=[TK,QR]`) seconds apart
flipped between `None` and a stable, byte-identical set of 4 flights (same
prices, times, airlines) across repeated calls. It's not that the data
varies — Google just sometimes serves an empty response for no reason.
`_run_search()` retries empty responses up to `route.get("search_retries", 3)`
times (2s delay between attempts) before logging "no results" as final.
Configurable per-route via `"search_retries"` in config.json — set to `0` to
disable.

### Multi-airport cities (`origin`/`destination` as a list)
`origin`/`destination` accept either a single IATA code (`"OTP"`) or a list of
codes (`["NRT", "HND"]`) to cover a multi-airport city — e.g. Tokyo (Narita +
Haneda), London (LHR/LGW/STN/...), New York (JFK/LGA/EWR). `_airport_codes()`/
`_airport_enums()` normalize the config value and `_build_filters()` passes all
listed airports into the same `FlightSegment` (`departure_airport`/
`arrival_airport` are natively `list[list[Airport, weight]]` — fli/Google
Flights search them together in one query, exactly like Google's own metro-area
search). `endpoint_label()` renders the route-level label as codes joined with
`/` (e.g. `"OTP → NRT/HND"`); each individual option in the dashboard shows the
*actual* airport it uses (`_render_option` reads `dep_code`/`arr_code` straight
off the parsed legs), so a NRT-bound and an HND-bound option are never confused.

### Multi-city (open-jaw) routes (`"trip_type": "multi_city"` + `"legs"`)
A route can be an **open-jaw** itinerary — fly out via one city pair and back
via a different one, e.g. `OTP → HKG` outbound, `NRT → OTP` return (you make
your own way from Hong Kong to Tokyo in between). Google prices this as a
*single combined itinerary* via its multi-city search — substantially cheaper
than booking the two one-ways separately, since they're ticketed together.

Set this up by replacing the route's top-level `origin`/`destination` with:
```json
"trip_type": "multi_city",
"legs": [
  { "origin": "OTP", "destination": "HKG" },
  { "origin": "NRT", "destination": "OTP" }
]
```
Currently **exactly 2 legs** are supported (`_route_legs()` raises if not) —
an open-jaw out-and-back, not arbitrary N-leg chains. Each leg's
`origin`/`destination` accepts the same single-code-or-list shape as a
round-trip route's (multi-airport cities work per leg too).

Everything else about the route config is unchanged and reused as-is:
- `date_from`/`date_to`/`daily_samples`/`departure_days` sample **leg 1's**
  departure date exactly like a round-trip's outbound.
- `target_nights`/`flexibility_days`/`return_days`/`max_return_date` generate
  **leg 2's** departure date relative to leg 1 — same `_return_dates()` logic,
  just read as "days until the journey home" rather than "nights at the
  destination" for an open-jaw trip.
- All filters (stops, layovers, airlines/alliances, duration caps,
  self-transfer, departure window, bags) apply identically.

Implementation-wise, `_is_multi_city()`/`_route_legs()` gate the branch points:
- `_build_filters()` builds one `FlightSegment` per leg with that leg's own
  airports (vs. the same pair flipped for round-trip) and sets
  `trip_type=TripType.MULTI_CITY`.
- **Price location differs by trip type** — Google returns the combined
  itinerary price on the *first* leg for round-trips but on the *final* leg
  for multi-city (mirrors fli's own CLI serialization, see
  `fli/cli/utils.py::display_flights` `price_segment` selection).
  `_parse_pair(outbound, return_flight, is_multi_city=...)` reads `price`
  (and `self_transfer`/`mixed_cabin`) off the correct segment accordingly;
  duration/stops are still summed across both legs either way.
- `route_endpoint_label()` renders the dual-airport label, e.g.
  `"OTP → HKG  /  NRT → OTP"`, used everywhere a route's endpoints are shown
  (messages, dashboard card badge/title). `_route_first_origin()`/
  `_route_final_destination()` give the itinerary's overall start/end airports
  for the dashboard tree's fallback display.

### Why search_country matters
Without `"search_country": "RO"`, Google returns generic results that may omit
airlines popular from Romania (e.g. Turkish Airlines). Setting it to `"RO"` mimics
searching from Romania and returns the same results as a manual search.

### Duration filtering is client-side
`max_outbound_duration_hours` and `max_return_duration_hours` are NOT passed to fli.
They filter results after the API call in `search_route()`. fli's `FlightSearchFilters`
only supports a single combined `max_duration`, not separate outbound/return limits.

### price_history.json size
Stores up to 30 flights per route per day, 180 days history.
Each flight includes full leg data for the dashboard tree view.

### Windows encoding
All file opens use `encoding="utf-8"` explicitly. Route labels contain `→` (U+2192)
which breaks Windows default cp1252 encoding. Log messages with emoji use the
UTF-8 stream handler workaround in the logging setup.

---

## Effective cost (time_value formula)

When `time_value.enabled = true` for a route, every flight gets an `effective_cost`
that normalises raw ticket price for time, vacation days, and discomfort — so a
cheap but painfully long flight can be compared fairly against a pricier comfortable
one. The result is used to sort flights and trigger alerts (`max_price_alert`
compares against `effective_cost`, not `price`). The dashboard chart shows both
lines.

### Full formula

```
effective_cost = price
  + _time_value_cost(extra_out, time_value_intervals)   ← symmetric, per leg
  + _time_value_cost(extra_ret, time_value_intervals)   ← negative = credit
  + vac_out                                              ← outbound vacation day
  + vac_ret                                              ← return vacation day
  + discomfort_out                                       ← tiered, positive extra only
  + discomfort_ret
```

Where:
- `extra_out = outbound_duration_h − base_outbound_duration_hours`
- `extra_ret = return_duration_h  − base_return_duration_hours`
- `_time_value_cost(x, intervals)`: tiered symmetric — positive extra → penalty,
  negative extra → credit at the same tier rates (mirrored)
- `discomfort_out/ret`: `_discomfort_cost(extra_h, discomfort_intervals)` —
  same tiered logic but only applied when `extra_h > 0` (no discomfort credit for
  short flights)

### Config fields

Shared fields live in `route_defaults.time_value`; per-route only needs `enabled`
+ `base_outbound_duration_hours` + `base_return_duration_hours` (plus any
other fields that should differ from defaults).

| Field | Where | Meaning |
|---|---|---|
| `enabled` | per-route | Switch on/off |
| `base_outbound_duration_hours` | per-route | Baseline outbound duration; `"HH:MM"` string or float |
| `base_return_duration_hours` | per-route | Baseline return duration; `"HH:MM"` string or float |
| `time_value_intervals` | route_defaults | Tiered rate list for symmetric time cost |
| `departure_threshold` | route_defaults | Outbound dep hour (e.g. `"18:00"`) — if dep hour < threshold, costs a vacation day |
| `vacation_day_cost_eur` | route_defaults | Cost of outbound vacation day |
| `return_arrival_vacation_from/to` | route_defaults | Return arrival window (e.g. `"09:00"`–`"15:00"`) — arrival inside = vacation day |
| `return_vacation_day_cost_eur` | route_defaults | Cost of return vacation day (defaults to `vacation_day_cost_eur`) |
| `discomfort_intervals` | route_defaults | Separate tiered penalty on positive extra hours per leg |

**`"HH:MM"` string parsing** (`_parse_duration_hours`): `"15:35"` → `15 + 35/60 = 15.5833h`.
Use exact fractions — don't round.

### Two separate interval lists

`time_value_intervals` and `discomfort_intervals` both use the same format
(`[{"from_hours": N, "to_hours": M, "rate_eur_per_hour": R}]`) but serve different
purposes:

- **`time_value_intervals`** — symmetric time value. Applied to every leg, positive
  or negative. First tier is typically a free tolerance zone (e.g. `[0–1h: €0]`)
  so minor deviations from baseline have zero cost. Beyond that, over-baseline costs
  and under-baseline credits at the same rate.
- **`discomfort_intervals`** — additional penalty on positive extra hours only.
  No credit for under-baseline (a short flight isn't more comfortable, just faster).

### Boundary rules (easy to get wrong)

- **Departure vacation** threshold is **strict `<`** — `dep_hour < threshold_h`.
  A 18:00 departure with `departure_threshold: "18:00"` does **not** trigger a
  vacation day (18 < 18 is False).
- **Return vacation** window is **inclusive on both ends** — `from_h <= ret_arr_hour <= to_h`.
- **`+1` in arrival time** (e.g. `06:40+1`) means next calendar day; the **hour is
  still the literal value shown** — `ret_arr_hour = 6`, not 30.
- **Discomfort** only fires when `extra_hours > 0`. Under-baseline legs get a
  time-value credit but zero discomfort.

### Worked example

Route `otp-hkd-2026` baselines: outbound `"13:20"` → 13.333h, return `"15:35"` → 15.583h.
Shared config (route_defaults): `time_value_intervals = [0–1h:€0, 1–99h:€25]`,
`vacation_day_cost_eur = 150`, threshold `18:00`, return window `09:00–15:00`,
`discomfort_intervals = [0–1h:€0, 1–2h:€10, 2–99h:€10]`.

**Flight: 941 EUR · OTP 16:20 → HKG 14:50+1 (16h30m) | HKG 18:05 → OTP 06:40+1 (18h35m)**

```
extra_out  = 16.5   − 13.333 = +3.167h  →  tv_out = 0 + 2.167×25 = €54.17
extra_ret  = 18.583 − 15.583 = +3.0h    →  tv_ret = 0 + 2.0×25   = €50.00
vac_out    = 150  (dep 16 < 18)
vac_ret    = 0    (arr 6, outside 9–15)
disc_out   = 0 + 1.167×10 = €11.67      (discomfort_intervals [1–2:10, 2–99:10])
disc_ret   = 0 + 1.0×10   = €10.00
eff = 941 + 54.17 + 50 + 150 + 0 + 11.67 + 10 = 1216.84 → 1217
```

**Baseline flight: 1046 EUR · OTP 21:45 → HKG 17:05+1 (13h20m) | HKG 23:15 → OTP 08:50+1 (15h35m)**
```
extra_out = 0, extra_ret = 0, vac_out = 0, vac_ret = 0 (arr 8 outside 9–15)
eff = 1046
```

---

## Debugging tools

### `_analyze_debug.py` — inspect raw fli output

`_analyze_debug.py` in the repo root analyses the `[outbound, return]` pair dumps
written to `debug/` when running with `--debug`. It groups pairs by
`(departure datetime, airline, price)` and prints how many return options each
distinct outbound candidate was expanded into — useful for answering "why does the
dashboard only show N options for airline X?".

Run `python flight_tracker.py --debug --route ROUTE_ID` first to generate dumps,
then point the script's `glob.glob(...)` patterns at the relevant files. Adjust
the pattern if you want to target a specific route or date range.

Example question it answers: "Turkish Airlines shows only 1 option per day" →
script confirms TK surfaces exactly 1 outbound candidate after filtering, because
`max_layover_hours: 5` / `max_outbound_duration_hours: 18` cut its other daily
departures before they reach expansion.

### `debug/run_reference.md` — per-run performance log

`debug/run_reference.md` tracks one entry per full GitHub Actions job run.
When job logs are pasted into the conversation, the expected workflow is:

1. Read `debug/run_reference.md` to see the previous entry format and values
2. Extract from the pasted log: total run time, per-route best price/date/airline,
   notification fired, 429 count, empty-retry volume, pairs with no results
3. Append a new dated entry in the same format
4. Call out notable changes vs the previous run (speed, API health, price movements)

The user may say "analyze this run" or just paste raw log output — either way,
read the file first before appending.
