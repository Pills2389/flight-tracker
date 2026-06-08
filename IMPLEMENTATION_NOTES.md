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
