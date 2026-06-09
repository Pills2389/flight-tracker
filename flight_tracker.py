#!/usr/bin/env python3
"""
✈️  Flight Price Tracker v3
─────────────────────────────────────────────────────────────
Data source  : fli (Google Flights) — no API key needed
               pip install flights click
Notifications: Email · WhatsApp (CallMeBot) · ntfy.sh
Storage      : price_history.json  (committed to GitHub repo)
Dashboard    : docs/index.html     → GitHub Pages
─────────────────────────────────────────────────────────────
"""

import json
import logging
import os
import smtplib
import sys
import time
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests
from fli.models import (
    Airline,
    Airport,
    Alliance,
    BagsFilter,
    FlightSearchFilters,
    FlightSegment,
    LayoverRestrictions,
    MaxStops,
    PassengerInfo,
    SortBy,
    TimeRestrictions,
    TripType,
)
from fli.search import SearchFlights

# ─────────────────────────────────────────────────────────────
# LOGGING
# ─────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler("flight_tracker.log", encoding="utf-8"),
        logging.StreamHandler(
            open(sys.stdout.fileno(), mode="w", encoding="utf-8", closefd=False)
        ),
    ],
)
log = logging.getLogger(__name__)

DEBUG      = False   # set to True via --debug flag
DEBUG_DIR  = Path("debug")

HISTORY_FILE   = "price_history.json"
DASHBOARD_DIR  = Path("docs")
DASHBOARD_FILE = DASHBOARD_DIR / "index.html"
DAYS_OF_WEEK   = ["monday","tuesday","wednesday","thursday","friday","saturday","sunday"]

# fli ships its own Airline enum and we can't patch it upstream — these are
# known-stale display names confirmed against the airline's own site for
# specific itineraries. IATA code VL used to be the Bulgarian charter "Air
# VIA"; it was reassigned ~20 months ago to the newly formed "Lufthansa City
# Airlines" (Air Dolomiti/Lufthansa CityLine merger), and fli's enum still
# carries the old name for that code. "Lufthansa Cargo" likewise shows up on
# plain Lufthansa passenger fares. Remap on display only — codes are untouched.
AIRLINE_NAME_FIXES = {
    "Lufthansa Cargo":  "Lufthansa",
    "Air VIA":          "Lufthansa City Airlines",
}


def _fix_airline_name(name: str) -> str:
    """Correct known-bad airline display names from fli's Airline enum."""
    return AIRLINE_NAME_FIXES.get(name, name)


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
def _merge_route_defaults(route: dict, defaults: dict) -> dict:
    """
    Fill in a route's missing fields from route_defaults — the route's own values always win.
    Dicts are merged key-by-key (recursively) rather than replaced wholesale, so a route can
    override e.g. just notifications.channels.whatsapp while still inheriting email/ntfy from
    the defaults; lists and scalars are replaced outright when the route specifies them.
    """
    merged = dict(defaults)
    for key, val in route.items():
        if isinstance(val, dict) and isinstance(merged.get(key), dict):
            merged[key] = _merge_route_defaults(val, merged[key])
        else:
            merged[key] = val
    return merged


def load_config() -> dict:
    env = os.environ.get("FLIGHT_CONFIG")
    if env:
        log.info("Loading config from FLIGHT_CONFIG env var")
        config = json.loads(env)
    elif Path("config.json").exists():
        with open("config.json", encoding="utf-8") as f:
            config = json.load(f)
    else:
        log.error("No config found. Copy config.example.json → config.json")
        sys.exit(1)

    defaults = config.get("route_defaults")
    if defaults:
        config["routes"] = [_merge_route_defaults(r, defaults) for r in config["routes"]]
    return config


# ─────────────────────────────────────────────────────────────
# AIRLINE / AIRPORT HELPERS
# ─────────────────────────────────────────────────────────────
def _get_preferred_airlines(route: dict) -> list[str]:
    """
    Return list of preferred airline IATA codes.
    Supports both new list format and old single-string format:
      "preferred_airlines": ["TK", "QR"]   <- new
      "preferred_airline":  "TK"            <- old (still works)
    """
    new = route.get("preferred_airlines", [])
    if isinstance(new, list) and new:
        return [a.upper() for a in new if a]
    old = route.get("preferred_airline", "")
    return [old.upper()] if old else []


def _airline_enums(codes: list[str]) -> list[Airline]:
    """Convert IATA airline codes to fli Airline enum members, skipping unknown ones."""
    out = []
    for code in codes:
        try:
            out.append(Airline[code.upper()])
        except KeyError:
            log.warning(f"Unknown airline code '{code}' — skipping from hard filter")
    return out


def _get_preferred_alliances(route: dict) -> list[str]:
    """Return list of preferred alliance names, e.g. ["ONEWORLD", "STAR_ALLIANCE"]."""
    alliances = route.get("preferred_alliances", [])
    if not isinstance(alliances, list):
        return []
    return [a.upper().replace(" ", "_") for a in alliances if a]


def _alliance_enums(names: list[str]) -> list[Alliance]:
    """Convert alliance names to fli Alliance enum members, skipping unknown ones."""
    out = []
    for name in names:
        try:
            out.append(Alliance[name])
        except KeyError:
            log.warning(f"Unknown alliance '{name}' — skipping from hard filter "
                        f"(valid: {', '.join(a.name for a in Alliance)})")
    return out


def _max_stops(n) -> MaxStops:
    """
    Map a stopover count to fli's MaxStops enum, mirroring fli's own
    CLI parsing (parse_max_stops): 0 -> non-stop, 1 -> one-or-fewer,
    2+ -> two-or-fewer. None/negative -> any.
    """
    if n is None:
        return MaxStops.ANY
    n = int(n)
    if n == 0:
        return MaxStops.NON_STOP
    if n == 1:
        return MaxStops.ONE_STOP_OR_FEWER
    if n >= 2:
        return MaxStops.TWO_OR_FEWER_STOPS
    return MaxStops.ANY


def _airport_codes(value) -> list[str]:
    """Normalize an origin/destination config value to a list of IATA codes.

    Accepts a single code ("OTP") or a list of codes (["NRT", "HND"]) — the
    latter lets one route cover a multi-airport city (e.g. Tokyo = NRT+HND)
    in a single search, since fli/Google Flights search multiple airports
    per segment natively.
    """
    codes = value if isinstance(value, list) else [value]
    return [c.upper() for c in codes]


def _airport_enums(codes: list[str]) -> list[Airport]:
    """Convert IATA codes to fli Airport enum members, skipping unknown ones."""
    out = []
    for code in codes:
        try:
            out.append(Airport[code])
        except KeyError:
            log.warning(f"Unknown airport code '{code}' — skipping")
    return out


def endpoint_label(value) -> str:
    """Display label for an origin/destination — joins multi-airport codes with '/'."""
    return "/".join(_airport_codes(value))


# ─────────────────────────────────────────────────────────────
# TIME-VALUE SCORING
# ─────────────────────────────────────────────────────────────
def _time_value_config(route: dict) -> dict | None:
    """Return the time_value config block if enabled, else None."""
    tv = route.get("time_value", {})
    return tv if tv.get("enabled", False) else None


def _outbound_time_score(outbound_dur_min: int, dep_hour: int | None, tv: dict) -> float:
    """Stage-1 time cost for pre-expansion outbound ranking (no round-trip price yet).

    Penalises outbound duration beyond the route's baseline and flights departing
    before the threshold that require taking a vacation day.
    """
    daily_rate  = float(tv.get("daily_rate_eur", 200))
    work_hours  = float(tv.get("work_hours_per_day", 8))
    base_out_h  = float(tv.get("base_outbound_duration_hours", 0))
    threshold_h = int(str(tv.get("departure_threshold", "18:00")).split(":")[0])
    vac_cost    = float(tv.get("vacation_day_cost_eur", daily_rate))
    hourly_rate = daily_rate / work_hours

    extra_out     = max(0.0, outbound_dur_min / 60 - base_out_h)
    vacation_cost = vac_cost if (dep_hour is not None and dep_hour < threshold_h) else 0.0
    return extra_out * hourly_rate + vacation_cost


def _compute_effective_cost(price: float, outbound_dur_min: int, return_dur_min: int,
                             dep_hour: int | None, tv: dict) -> float:
    """Full effective cost = price + time_penalty(outbound + return) + vacation_cost(outbound).

    extra_hours are measured against per-direction baselines; only hours above
    the baseline cost money so the baseline flight (e.g. the known TK routing)
    adds zero penalty.  Vacation cost fires only when outbound departs before
    the threshold — early departure = need an extra vacation day.
    """
    daily_rate  = float(tv.get("daily_rate_eur", 200))
    work_hours  = float(tv.get("work_hours_per_day", 8))
    base_out_h  = float(tv.get("base_outbound_duration_hours", 0))
    base_ret_h  = float(tv.get("base_return_duration_hours", 0))
    threshold_h = int(str(tv.get("departure_threshold", "18:00")).split(":")[0])
    vac_cost    = float(tv.get("vacation_day_cost_eur", daily_rate))
    hourly_rate = daily_rate / work_hours

    extra_out     = max(0.0, outbound_dur_min / 60 - base_out_h)
    extra_ret     = max(0.0, return_dur_min   / 60 - base_ret_h)
    vacation_cost = vac_cost if (dep_hour is not None and dep_hour < threshold_h) else 0.0
    return round(price + (extra_out + extra_ret) * hourly_rate + vacation_cost, 2)


# ─────────────────────────────────────────────────────────────
# MULTI-CITY (open-jaw) ROUTE HELPERS
# ─────────────────────────────────────────────────────────────
def _is_multi_city(route: dict) -> bool:
    """True for open-jaw routes — e.g. fly OTP→HKG, return NRT→OTP.

    Such routes use `"trip_type": "multi_city"` plus a `"legs"` array of
    exactly two `{origin, destination}` pairs instead of the single
    top-level `origin`/`destination` a round-trip route uses. Unlike a
    round-trip search (same airports both ways), this maps to fli's
    TripType.MULTI_CITY so Google prices the whole open-jaw itinerary
    together — much cheaper than booking the two one-ways separately.
    """
    return route.get("trip_type", "round_trip") == "multi_city"


def _route_legs(route: dict) -> list[dict]:
    """Return the `{origin, destination}` legs of a multi-city route."""
    legs = route.get("legs", [])
    if len(legs) != 2:
        raise ValueError(
            f"Route '{route.get('id','?')}': multi_city requires exactly 2 legs "
            f"(open-jaw out + back), got {len(legs)}"
        )
    return legs


def _route_first_origin(route: dict):
    """Origin of the very first leg — where the whole itinerary starts."""
    return _route_legs(route)[0]["origin"] if _is_multi_city(route) else route["origin"]


def _route_final_destination(route: dict):
    """Destination of the very last leg — where the itinerary ends."""
    return _route_legs(route)[-1]["destination"] if _is_multi_city(route) else route["destination"]


def route_endpoint_label(route: dict) -> str:
    """Display label for a route's full itinerary.

    Round-trip: "OTP → AKL". Multi-city (open-jaw): "OTP → HKG  /  NRT → OTP"
    — each leg shown with its own origin/destination since they differ.
    """
    if _is_multi_city(route):
        return "  /  ".join(
            f"{endpoint_label(leg['origin'])} → {endpoint_label(leg['destination'])}"
            for leg in _route_legs(route)
        )
    return f"{endpoint_label(route['origin'])} → {endpoint_label(route['destination'])}"


# ─────────────────────────────────────────────────────────────
# FLI — BUILD SEARCH FILTERS
# ─────────────────────────────────────────────────────────────
def _build_filters(route: dict, dates: list[str]) -> FlightSearchFilters:
    """Build a typed FlightSearchFilters for one date combination.

    `dates` holds one travel date per leg, in order:
    `[departure, return]` for a round-trip route, or one date per entry
    in `route["legs"]` for a multi-city (open-jaw) route.

    Round-trip uses the same airports both ways (TripType.ROUND_TRIP).
    Multi-city lets each leg have its own origin/destination — e.g.
    OTP→HKG out, NRT→OTP back — and must use TripType.MULTI_CITY so
    Google prices the whole open-jaw itinerary as one ticket.
    """
    dw = route.get("departure_window", {})
    outbound_restrictions = None
    if dw.get("enabled") and dw.get("mode") == "hard":
        outbound_restrictions = TimeRestrictions(
            earliest_departure=int(dw["from"].split(":")[0]),
            latest_departure=int(dw["to"].split(":")[0]),
        )

    if _is_multi_city(route):
        trip_type = TripType.MULTI_CITY
        leg_endpoints = [
            (_airport_enums(_airport_codes(leg["origin"])),
             _airport_enums(_airport_codes(leg["destination"])))
            for leg in _route_legs(route)
        ]
    else:
        trip_type = TripType.ROUND_TRIP
        origins      = _airport_enums(_airport_codes(route["origin"]))
        destinations = _airport_enums(_airport_codes(route["destination"]))
        leg_endpoints = [(origins, destinations), (destinations, origins)]

    segments = [
        FlightSegment(
            departure_airport=[[a, 0] for a in dep],
            arrival_airport=[[a, 0] for a in arr],
            travel_date=date,
            # Departure-window restriction only ever applies to the very
            # first leg (the outbound flight from home).
            time_restrictions=outbound_restrictions if i == 0 else None,
        )
        for i, ((dep, arr), date) in enumerate(zip(leg_endpoints, dates))
    ]

    layover_restrictions = None
    max_lay = route.get("max_layover_hours")
    if max_lay:
        layover_restrictions = LayoverRestrictions(max_duration=int(float(max_lay) * 60))

    bags = route.get("bags", 0)

    # Hard airline/alliance filters are passed to fli directly (show only
    # matching carriers), otherwise we'd risk missing the preferred carrier
    # entirely (it might not be among the cheapest options fli expands)
    al_mode   = route.get("preferred_airline_mode", "soft")
    airlines  = None
    alliances = None
    if al_mode == "hard":
        preferred = _get_preferred_airlines(route)
        if preferred:
            airlines = _airline_enums(preferred) or None
        preferred_alliances = _get_preferred_alliances(route)
        if preferred_alliances:
            alliances = _alliance_enums(preferred_alliances) or None

    return FlightSearchFilters(
        trip_type=trip_type,
        passenger_info=PassengerInfo(adults=route.get("passengers", 1)),
        flight_segments=segments,
        stops=_max_stops(route.get("max_stopovers")),
        airlines=airlines,
        alliances=alliances,
        layover_restrictions=layover_restrictions,
        bags=BagsFilter(checked_bags=bags) if bags else None,
        sort_by=SortBy.CHEAPEST,
    )


def _prefilter_outbound(flights: list, route: dict, is_multi_city: bool) -> tuple[list, int, int]:
    """Drop raw outbound FlightResults that search_route()'s client-side
    filters would discard anyway, before they cost an _expand_multi_leg slot.

    Only filters that are decidable from the outbound leg alone are safe here:
    `max_outbound_duration_hours` (FlightResult.duration is leg-1's duration
    for both round-trip and multi-city) and, for round-trip routes only,
    `exclude_self_transfer` (FlightResult.self_transfer — _parse_pair reads
    the combined-trip flag off `outbound` for round-trip but off the final/
    return leg for multi-city, so pre-checking it on the raw outbound would
    use the wrong segment's flag for multi-city and risk dropping good
    itineraries).

    Returns (kept, skipped_duration, skipped_self_transfer). `kept` preserves
    fli's cheapest-first order — _rank_outbound reorders it next.
    """
    max_out_h = route.get("max_outbound_duration_hours")
    exclude_self_transfer = route.get("exclude_self_transfer", True)
    use_hard_duration = not _time_value_config(route)

    kept, skip_dur, skip_self = [], 0, 0
    for f in flights:
        if use_hard_duration and max_out_h and f.duration > max_out_h * 60:
            skip_dur += 1
            continue
        if not is_multi_city and exclude_self_transfer and f.self_transfer:
            skip_self += 1
            continue
        kept.append(f)
    return kept, skip_dur, skip_self


def _rank_outbound(flights: list, route: dict) -> list:
    """Reorder surviving outbound candidates so _expand_multi_leg's top_n
    slots go to the flights this route is actually configured to prefer —
    cheapest within each preference tier — instead of blindly cheapest-overall.
    Best price isn't everything; an off-hours flight on a non-preferred
    carrier costs time even when it's a few EUR cheaper.

    Tiers (lower = expanded first): 0 = matches preferred airline/alliance AND
    departure window, 1 = airline only, 2 = window only, 3 = neither. Routes
    with neither preference configured put every candidate in tier 0, so the
    sort collapses back to plain price — unchanged behaviour for those routes.

    Mirrors the match logic search_route() already computes post-hoc as
    `preferred_airline_match` / `preferred_time`, just evaluated on the raw
    FlightResult one step earlier (airline codes off `leg.airline.name`,
    departure hour off `legs[0].departure_datetime`).
    """
    preferred = _get_preferred_airlines(route)
    al_mode   = route.get("preferred_airline_mode", "soft")
    check_airline = bool(preferred) and al_mode != "off"

    dw = route.get("departure_window", {})
    check_window = bool(dw.get("enabled"))
    fh = int(dw["from"].split(":")[0]) if check_window else None
    th = int(dw["to"].split(":")[0]) if check_window else None

    tv = _time_value_config(route)

    def tier(f) -> tuple[int, float]:
        legs = f.legs or []
        airline_ok = (not check_airline) or any(
            leg.airline.name in preferred for leg in legs
        )
        window_ok = (not check_window) or (
            bool(legs) and fh <= legs[0].departure_datetime.hour <= th
        )
        penalty = (0 if airline_ok else 2) + (0 if window_ok else 1)

        if tv:
            dep_hour = legs[0].departure_datetime.hour if legs else None
            score    = _outbound_time_score(f.duration, dep_hour, tv)
        else:
            score = f.price if f.price is not None else float("inf")

        return (penalty, score)

    return sorted(flights, key=tier)


# ─────────────────────────────────────────────────────────────
# FLI — RUN & PARSE
# ─────────────────────────────────────────────────────────────
def _run_search(search: SearchFlights, route: dict, dates: list[str],
                top_n: int, debug_label: str = "") -> tuple[list[dict], dict]:
    """Run one two-leg search via the fli Python API and return parsed flights
    plus the outbound funnel counts {raw, kept, filtered} from
    `_prefilter_outbound` (for the per-date-pair summary log line in
    `search_route`).

    `dates` is `[departure, return]` — the travel date for each leg, in the
    same order as the segments `_build_filters` builds (round-trip: same
    airports both ways; multi-city/open-jaw: each leg's own airports from
    `route["legs"]`).

    Google's backend occasionally returns an empty payload for a query that
    returns stable, non-empty results moments later (a transient glitch, not
    a sign the route/dates have no flights — confirmed by hand: identical
    retries return byte-identical flight data). We retry empty responses a
    few times before accepting "no results" as final.
    """
    filters = _build_filters(route, dates)
    retries = route.get("search_retries", 3)
    leg_label = " → ".join(dates)

    # We call fli's _fetch_flights/_expand_multi_leg directly (not search())
    # so we can drop outbound candidates our own filters would discard anyway
    # — and rank the survivors by route preference (airline/alliance,
    # departure window) rather than pure cheapest-first — before spending
    # _expand_multi_leg's top_n slots on them. See CLAUDE.md "Outbound
    # candidate selection" for the rationale; these are private fli methods,
    # so a future fli upgrade reshaping them needs to be re-checked here.
    is_multi_city = _is_multi_city(route)
    results = None
    outbound_stats = {"raw": 0, "kept": 0, "filtered": 0}
    for attempt in range(retries + 1):
        try:
            outbound = search._fetch_flights(
                filters,
                currency=route.get("currency", "EUR"),
                language=route.get("search_language") or None,
                country=route.get("search_country") or None,
                capture_session=True,
            )
            if outbound is None:
                results = None
            else:
                kept, skip_dur, skip_self = _prefilter_outbound(outbound, route, is_multi_city)
                outbound_stats = {"raw": len(outbound), "kept": len(kept),
                                  "filtered": skip_dur + skip_self}
                if DEBUG and (skip_dur or skip_self):
                    log.info(f"  [DEBUG] Outbound pre-filter: {len(outbound)} raw → "
                             f"{skip_dur} too long, {skip_self} self-transfer  → {len(kept)} kept")
                ranked = _rank_outbound(kept, route)
                results = search._expand_multi_leg(
                    ranked,
                    filters,
                    top_n=top_n,
                    currency=route.get("currency", "EUR"),
                    language=route.get("search_language") or None,
                    country=route.get("search_country") or None,
                )
        except Exception as e:
            log.error(f"fli search error: {e}")
            results = None

        if results:
            break
        if attempt < retries:
            log.info(f"  {leg_label}: empty response, "
                     f"retrying ({attempt + 1}/{retries})…")
            time.sleep(2)

    if not results:
        return [], outbound_stats

    pairs = [r for r in results if isinstance(r, tuple) and len(r) == 2]

    # ── Debug: save raw API response to file ─────────────────
    if DEBUG and debug_label:
        DEBUG_DIR.mkdir(exist_ok=True)
        ts       = datetime.now().strftime("%H%M%S")
        filename = DEBUG_DIR / f"{debug_label}_{ts}.json"
        dump = [[leg.model_dump(mode="json") for leg in pair] for pair in pairs]
        filename.write_text(json.dumps(dump, indent=2, ensure_ascii=False),
                            encoding="utf-8")
        log.info(f"  [DEBUG] Saved {len(pairs)} raw results → {filename}")

    is_multi_city = _is_multi_city(route)
    out = []
    for outbound, return_flight in pairs:
        try:
            f = _parse_pair(outbound, return_flight, is_multi_city=is_multi_city)
            if f:
                # Kept around (and stripped before serialization, see process_route's
                # top_flights build) so _filter_airline_direct can later call
                # search.get_booking_options() on this exact itinerary without
                # re-running the search.
                f["raw"] = (outbound, return_flight)
                out.append(f)
        except Exception as e:
            log.debug(f"Skipping unparseable flight: {e}")
    return out, outbound_stats


def _parse_pair(outbound, return_flight, is_multi_city: bool = False) -> dict | None:
    """
    Build a normalized flight dict from a (leg1, leg2) FlightResult pair —
    a round-trip outbound+return, or a 2-leg open-jaw multi-city itinerary.

    Mirrors fli's own CLI serialization (see fli/cli/utils.py
    `display_flights`): the combined trip price, currency, duration and stop
    count live on the *first* leg for round-trips, but on the *final* leg for
    multi-city — Google surfaces the all-in itinerary price differently for
    each trip type, so we must read it off the right segment.
    """
    price_segment = return_flight if is_multi_city else outbound
    if price_segment.price is None:
        return None
    price = float(price_segment.price)

    out_legs = outbound.legs or []
    ret_legs = return_flight.legs or []
    out_lays = outbound.layovers or []
    ret_lays = return_flight.layovers or []

    outbound_dur = outbound.duration
    return_dur   = return_flight.duration
    duration_min = outbound_dur + return_dur
    stops        = outbound.stops + return_flight.stops

    # ── Airlines & departure time (outbound, first leg) ──────
    airlines      = []
    airline_codes = []
    dep_hour      = None
    dep_time_str  = ""

    for i, leg in enumerate(out_legs):
        al_name = _fix_airline_name(leg.airline.value)
        al_code = leg.airline.name
        if al_name and al_name not in airlines:
            airlines.append(al_name)
        if al_code and al_code not in airline_codes:
            airline_codes.append(al_code)
        if i == 0:
            dep_hour     = leg.departure_datetime.hour
            dep_time_str = leg.departure_datetime.strftime("%H:%M")

    # ── Max layover (outbound only) ──────────────────────────
    max_layover_min = max((lay.duration for lay in out_lays), default=0)

    # ── Arrival/departure times for tree display ─────────────
    outbound_arr_time = ""
    if out_legs:
        outbound_arr_time = _time_offset(out_legs[-1].arrival_datetime,
                                         out_legs[0].departure_datetime)

    return_dep_time = ""
    return_arr_time = ""
    if ret_legs:
        ret_ref         = ret_legs[0].departure_datetime
        return_dep_time = ret_ref.strftime("%H:%M")
        return_arr_time = _time_offset(ret_legs[-1].arrival_datetime, ret_ref)

    # ── Leg details for dashboard tree ───────────────────────
    outbound_legs = _extract_legs(out_legs, out_lays)
    return_legs   = _extract_legs(ret_legs, ret_lays)

    return {
        "price":                  round(price, 2),
        "duration_min":           duration_min,
        "duration_str":           _minutes_to_hm(duration_min),
        "outbound_duration_min":  outbound_dur,
        "outbound_duration_str":  _minutes_to_hm(outbound_dur),
        "return_duration_min":    return_dur,
        "return_duration_str":    _minutes_to_hm(return_dur) if return_dur else "",
        "stops":                  stops,
        "airline":                ", ".join(airlines) if airlines else "Unknown",
        "airline_codes":          airline_codes,
        "max_layover_min":        max_layover_min,
        "max_layover_h":          round(max_layover_min / 60, 1),
        "dep_hour":               dep_hour,
        "dep_time":               dep_time_str,
        "outbound_arr_time":      outbound_arr_time,
        "return_dep_time":        return_dep_time,
        "return_arr_time":        return_arr_time,
        "outbound_legs":          outbound_legs,
        "return_legs":            return_legs,
        "self_transfer":          bool(price_segment.self_transfer),
    }


def _minutes_to_hm(m: int) -> str:
    if not m:
        return "?"
    return f"{m // 60}h {m % 60:02d}m"


# ─────────────────────────────────────────────────────────────
# FLI — HIGH-LEVEL ROUTE SEARCH
# ─────────────────────────────────────────────────────────────
def _sample_dates(date_from: str, date_to: str, n: int,
                  departure_days: list | None = None) -> list[str]:
    """
    Returns departure dates to check.

    - If departure_days is set: return ALL dates in the window that fall
      on those weekdays. daily_samples is ignored — you asked for specific
      days, so we check all of them.
    - If departure_days is not set: sample n evenly-spaced dates across
      the window (original behaviour).
    """
    start = datetime.strptime(date_from, "%Y-%m-%d")
    end   = datetime.strptime(date_to,   "%Y-%m-%d")

    if departure_days:
        allowed = {d.lower() for d in departure_days}
        dates = []
        cur = start
        while cur <= end:
            if cur.strftime("%A").lower() in allowed:
                dates.append(cur.strftime("%Y-%m-%d"))
            cur += timedelta(days=1)
        if not dates:
            log.warning("No dates match departure_days in the window — falling back to daily_samples")
        else:
            log.info(f"  Departure days filter: {len(dates)} matching dates across window")
            return dates

    # No departure_days set — sample evenly
    span = (end - start).days
    if span < 0:
        return [date_from]
    if span == 0 or n == 1:
        return [date_from]
    step = span / (n - 1)
    dates = []
    for i in range(n):
        d = start + timedelta(days=round(i * step))
        dates.append(d.strftime("%Y-%m-%d"))
    return sorted(set(dates))


def _return_dates(departure: str, route: dict) -> list[str]:
    """
    Generate return date candidates for a given departure.

    - If return_days is set: check every night from (target - flex) to
      (target + flex) and keep only those landing on the preferred weekdays.
    - If return_days is not set: return just target-flex, target, target+flex.
    - Respects max_return_date if set — no return date beyond that is generated.
    Falls back to exact target date if nothing matches.
    """
    dep         = datetime.strptime(departure, "%Y-%m-%d")
    base        = route.get("target_nights", 20)
    flex        = route.get("flexibility_days", 0)
    return_days = {d.lower() for d in route.get("return_days", [])}
    max_ret     = route.get("max_return_date")  # e.g. "2026-10-31"

    candidates = []
    for nights in range(max(1, base - flex), base + flex + 1):
        ret     = dep + timedelta(days=nights)
        ret_str = ret.strftime("%Y-%m-%d")
        if max_ret and ret_str > max_ret:
            continue
        if not return_days or ret.strftime("%A").lower() in return_days:
            candidates.append(ret_str)

    if not candidates:
        fallback = (dep + timedelta(days=base)).strftime("%Y-%m-%d")
        if max_ret and fallback > max_ret:
            return []   # no valid return date exists — skip this departure
        log.debug(f"  No return day match for dep {departure}, using fallback {fallback}")
        return [fallback]

    return sorted(set(candidates))


def _filter_airline_direct(flights: list[dict], route: dict, search: SearchFlights,
                           limit: int = 30, max_checks: int = 60) -> list[dict]:
    """Drop itineraries that aren't bookable directly with an airline (only
    agency/OTA fares), walking the price-sorted candidates and keeping the
    cheapest `limit` survivors — agency-only fares are simply skipped, so the
    next-cheapest candidate naturally fills their slot.

    Single-carrier round trips are assumed airline-direct without spending an
    API call — fli has no airline→alliance membership data to do better than
    "same airline" cheaply (see CLAUDE.md "Why alliances can't be flagged"),
    and a single carrier covering every leg is overwhelmingly likely to be
    sellable on that airline's own site. Multi-carrier itineraries (the
    "Frankenstein" combos OTAs assemble from interline fares) are checked for
    real via fli's GetBookingResults — capped at `max_checks` since each is a
    separate API call.
    """
    kept   = []
    checks = 0
    for f in flights:
        if len(kept) >= limit:
            break

        raw = f.get("raw")
        if raw is None:
            kept.append(f)
            continue

        codes = {leg.airline.name for leg in (raw[0].legs or [])} | \
                {leg.airline.name for leg in (raw[1].legs or [])}

        if len(codes) <= 1:
            f["airline_direct"] = True
        elif checks >= max_checks:
            continue  # over budget — skip rather than guess
        else:
            checks += 1
            try:
                filters = _build_filters(route, [f["outbound_date"], f["return_date"]])
                token   = raw[-1].booking_token or raw[0].booking_token
                options = search.get_booking_options(
                    raw, filters,
                    currency=route.get("currency", "EUR"),
                    language=route.get("search_language") or None,
                    country=route.get("search_country") or None,
                    booking_token=token,
                )
                direct = next((o for o in options if o.is_airline_direct), None)
                f["airline_direct"] = direct is not None
                f["vendor"] = direct.vendor_name if direct else (
                    options[0].vendor_name if options else None)
            except Exception as e:
                # Fail open — a flaky booking-options call shouldn't disqualify
                # an otherwise-good fare.
                log.debug(f"  get_booking_options failed for {f.get('airline')}: {e}")
                f["airline_direct"] = True

        if f["airline_direct"]:
            kept.append(f)
        elif DEBUG:
            log.info(f"  [DEBUG] Dropped agency-only fare: {f['price']:.0f} "
                     f"{route.get('currency','EUR')} {f.get('airline')} "
                     f"(vendor: {f.get('vendor','?')})")

    if DEBUG and checks:
        log.info(f"  [DEBUG] Airline-direct check: {checks} multi-carrier "
                 f"itineraries verified via get_booking_options")

    return kept


def search_route(route: dict) -> tuple[list[dict], dict]:
    """
    Sample departure dates across the window and return all flight
    options found (enriched with outbound/return date info), plus a
    raw API health rollup: {pairs_searched, pairs_with_results,
    raw_flights_total} — counted *before* client-side filtering, so a
    drop to zero signals the API itself going quiet rather than our
    filters getting stricter.

    Date pairs are searched in parallel (up to 4 workers), each with
    its own SearchFlights instance — required because SearchFlights
    caches a session id on self and is not thread-safe. Results are
    sorted by date before post-processing so log output is deterministic.
    """
    is_multi_city = _is_multi_city(route)
    if is_multi_city:
        _route_legs(route)  # validates exactly 2 legs, raises with a clear message otherwise

    n_samples = route.get("daily_samples", 8)
    dep_dates = _sample_dates(
        route["date_from"], route["date_to"], n_samples,
        departure_days=route.get("departure_days") or None
    )

    dw    = route.get("departure_window", {})
    top_n = route.get("top_n", 20)

    api_stats = {"pairs_searched": 0, "pairs_with_results": 0, "raw_flights_total": 0}

    # Build the full list of (dep, ret) pairs upfront
    date_pairs = [
        (dep, ret)
        for dep in dep_dates
        for ret in _return_dates(dep, route)
        if (datetime.strptime(ret, "%Y-%m-%d") - datetime.strptime(dep, "%Y-%m-%d")).days >= 1
    ]

    def _search_pair(pair: tuple[str, str]) -> tuple[str, str, int, list[dict], dict]:
        dep, ret = pair
        nights = (datetime.strptime(ret, "%Y-%m-%d") - datetime.strptime(dep, "%Y-%m-%d")).days
        label  = f"{route['id']}_{dep}_{ret}"
        log.info(f"  → {dep} → {ret} ({nights}n): searching…")
        s = SearchFlights()
        flights, ob_stats = _run_search(s, route, [dep, ret], top_n,
                                        debug_label=label if DEBUG else "")
        return dep, ret, nights, flights, ob_stats

    # Parallel search phase — workers log their start immediately so the
    # console stays live; results arrive out of order and are sorted below.
    n_workers = min(4, len(date_pairs)) if date_pairs else 1
    with ThreadPoolExecutor(max_workers=n_workers) as executor:
        raw_results = list(executor.map(_search_pair, date_pairs))

    # Sort by (dep, ret) so per-pair log lines and results are date-ordered
    raw_results.sort(key=lambda x: (x[0], x[1]))

    results = []
    tv = _time_value_config(route)

    for dep, ret, nights, flights, ob_stats in raw_results:
        api_stats["pairs_searched"] += 1
        api_stats["raw_flights_total"] += len(flights) if flights else 0
        if flights:
            api_stats["pairs_with_results"] += 1

        if not flights:
            log.info(f"  {dep} → {ret}: no results")
            continue

        currency      = route.get("currency", "EUR")
        prices        = [f["price"] for f in flights]
        airline_count = len({code for f in flights for code in f.get("airline_codes", [])})
        options_label = "multi-city options" if is_multi_city else "round-trip options"
        log.info(f"  {dep} → {ret} ({nights}n): "
                 f"outbound {ob_stats['raw']} → {ob_stats['kept']} kept ({ob_stats['filtered']} filtered) → "
                 f"{len(flights)} {options_label}, "
                 f"{min(prices):.0f}–{max(prices):.0f} {currency} across {airline_count} airlines, "
                 f"best {flights[0]['price']:.0f} {currency}")

        # Debug: show airline breakdown before filtering
        if DEBUG:
            airline_counts: dict[str, int] = {}
            for f in flights:
                for code in f.get("airline_codes", ["?"]):
                    airline_counts[code] = airline_counts.get(code, 0) + 1
            log.info(f"  [DEBUG] Airlines in raw results: "
                     f"{', '.join(f'{k}:{v}' for k,v in sorted(airline_counts.items()))}")

        filtered_in  = 0
        skip_dur_out = 0
        skip_dur_ret = 0
        skip_self_tr = 0

        for f in flights:
            f["outbound_date"] = dep
            f["return_date"]   = ret
            f["nights"]        = nights
            f["origin"]        = endpoint_label(_route_first_origin(route))
            f["destination"]   = endpoint_label(_route_final_destination(route))

            # Duration filters — skipped when time_value is active (effective_cost handles it)
            if not tv:
                max_out_h = route.get("max_outbound_duration_hours")
                max_ret_h = route.get("max_return_duration_hours")
                if max_out_h and f["outbound_duration_min"] > max_out_h * 60:
                    skip_dur_out += 1
                    continue
                if max_ret_h and f["return_duration_min"] > max_ret_h * 60:
                    skip_dur_ret += 1
                    continue

            # Self-transfer filter
            f["self_transfer"] = f.get("self_transfer", False)
            if route.get("exclude_self_transfer", True) and f["self_transfer"]:
                skip_self_tr += 1
                continue

            # Preferred airline flag
            preferred_list = _get_preferred_airlines(route)
            al_mode        = route.get("preferred_airline_mode", "soft")
            if preferred_list and al_mode != "off":
                f["preferred_airline_match"] = any(
                    code in f.get("airline_codes", [])
                    for code in preferred_list
                )
            else:
                f["preferred_airline_match"] = False

            # Soft departure-window flag
            if dw.get("enabled"):
                fh = int(dw["from"].split(":")[0])
                th = int(dw["to"].split(":")[0])
                f["preferred_time"] = (
                    f["dep_hour"] is not None and
                    fh <= f["dep_hour"] <= th
                )
            else:
                f["preferred_time"] = False

            filtered_in += 1
            results.append(f)

        if DEBUG and (skip_dur_out or skip_dur_ret or skip_self_tr):
            log.info(f"  [DEBUG] Filtered out: "
                     f"{skip_dur_out} outbound too long, "
                     f"{skip_dur_ret} return too long, "
                     f"{skip_self_tr} self-transfer  "
                     f"→ {filtered_in} kept")

    if tv:
        hourly_rate = float(tv.get("daily_rate_eur", 200)) / float(tv.get("work_hours_per_day", 8))
        threshold_h = int(str(tv.get("departure_threshold", "18:00")).split(":")[0])
        for f in results:
            f["effective_cost"] = _compute_effective_cost(
                f["price"], f["outbound_duration_min"], f["return_duration_min"],
                f.get("dep_hour"), tv,
            )
        results.sort(key=lambda x: x["effective_cost"])
        if DEBUG and results:
            best = results[0]
            vac = float(tv.get("vacation_day_cost_eur", tv.get("daily_rate_eur", 200)))
            vac_applied = vac if (best.get("dep_hour") is not None and best["dep_hour"] < threshold_h) else 0
            extra_out = max(0.0, best["outbound_duration_min"] / 60 - float(tv.get("base_outbound_duration_hours", 0)))
            extra_ret = max(0.0, best["return_duration_min"] / 60 - float(tv.get("base_return_duration_hours", 0)))
            log.info(f"  [DEBUG] time_value active — best eff. cost: "
                     f"{best['effective_cost']:.0f} = {best['price']:.0f} price "
                     f"+ {(extra_out + extra_ret) * hourly_rate:.0f} time penalty "
                     f"({extra_out:.1f}h out + {extra_ret:.1f}h ret × {hourly_rate:.0f}€/h) "
                     f"+ {vac_applied:.0f} vacation ({best.get('dep_time','?')} dep)")
    else:
        results.sort(key=lambda x: x["price"])

    # Redundant client-side re-check for the hard airline filter (defence in
    # depth in case fli's server-side filter misbehaves). Skipped when an
    # alliance filter is also active — we have no airline→alliance membership
    # data to verify those matches client-side, so we trust fli's combined
    # airlines+alliances query instead.
    preferred_list      = _get_preferred_airlines(route)
    preferred_alliances = _get_preferred_alliances(route)
    al_mode             = route.get("preferred_airline_mode", "soft")
    if al_mode == "hard" and preferred_list and not preferred_alliances:
        before  = len(results)
        results = [f for f in results if f.get("preferred_airline_match")]
        log.info(f"  Hard airline filter ({', '.join(preferred_list)}): {before} → {len(results)} flights")

    # Drop agency/OTA-only fares by default (e.g. multi-carrier interline
    # combos you can't manage directly with an airline if something goes
    # wrong) — set allow_agency_fares: true on a route to skip this and see
    # them too. See _filter_airline_direct.
    if not route.get("allow_agency_fares"):
        before  = len(results)
        results = _filter_airline_direct(results, route, SearchFlights())
        log.info(f"  Airline-direct filter: {before} → {len(results)} flights")

    return results, api_stats


# ─────────────────────────────────────────────────────────────
# PRICE HISTORY  (price_history.json)
# ─────────────────────────────────────────────────────────────
def load_history() -> dict:
    if Path(HISTORY_FILE).exists():
        try:
            with open(HISTORY_FILE, encoding="utf-8") as f:
                content = f.read().strip()
                return json.loads(content) if content else {}
        except (json.JSONDecodeError, Exception) as e:
            log.warning(f"Could not read history file: {e} — starting fresh")
    return {}


def save_history(history: dict):
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2, ensure_ascii=False)


def _today() -> str:
    return datetime.now().strftime("%Y-%m-%d")


def add_entry(history: dict, rid: str, label: str, entry: dict):
    if rid not in history:
        history[rid] = {
            "label": label,
            "entries": [],
            "last_weekly_summary": None,
            "last_alert_date": None,
            "last_alert_price": None,
            "last_daily_date": None,
            "last_daily_price": None,
        }
    entries = history[rid]["entries"]
    # Older entries are never read back beyond date/best_price (see analyze_trend
    # and the dashboard's best-ever/chart calcs) — drop their top_flights/leg data
    # so history doesn't carry ~30 full itineraries per route per day forever.
    entries = [
        {"date": e["date"], "best_price": e["best_price"]}
        for e in entries
        if e["date"] != _today()
    ]
    entries.append(entry)
    history[rid]["entries"] = entries[-180:]


def recent_entries(history: dict, rid: str, days: int = 30) -> list:
    if rid not in history:
        return []
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return [e for e in history[rid]["entries"] if e["date"] >= cutoff]


def add_api_stats(history: dict, rid: str, label: str, stats: dict):
    """Record today's raw (pre-filter) API result counts for this route —
    independent of whether any flights survived filtering, so a weekly
    heartbeat can still report on API health on a zero-result week."""
    if rid not in history:
        history[rid] = {
            "label": label,
            "entries": [],
            "last_weekly_summary": None,
            "last_alert_date": None,
            "last_alert_price": None,
            "last_daily_date": None,
            "last_daily_price": None,
        }
    health = [h for h in history[rid].get("api_health", []) if h["date"] != _today()]
    health.append({"date": _today(), **stats})
    history[rid]["api_health"] = health[-30:]


def api_health_summary(history: dict, rid: str, days: int = 7) -> dict | None:
    """Roll up the last N days of raw API result counts into a single
    summary for the weekly report, or None if there's no data yet."""
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    recent = [h for h in history.get(rid, {}).get("api_health", []) if h["date"] >= cutoff]
    if not recent:
        return None
    pairs_searched     = sum(h["pairs_searched"] for h in recent)
    pairs_with_results = sum(h["pairs_with_results"] for h in recent)
    raw_flights_total  = sum(h["raw_flights_total"] for h in recent)
    return {
        "days":               len(recent),
        "pairs_searched":     pairs_searched,
        "pairs_with_results": pairs_with_results,
        "raw_flights_total":  raw_flights_total,
        "avg_per_search":     round(raw_flights_total / pairs_searched, 1) if pairs_searched else 0,
    }


# ─────────────────────────────────────────────────────────────
# TREND ANALYSIS
# ─────────────────────────────────────────────────────────────
def analyze_trend(entries: list, currency: str) -> dict:
    empty = {"direction": "unknown", "signal": "📊 Not enough data yet",
             "pct_7d": None, "avg_7d": None, "avg_14d": None, "consecutive": 0}
    if len(entries) < 2:
        return empty

    by_date  = {e["date"]: e["best_price"] for e in entries}
    dates    = sorted(by_date)
    today_px = by_date[dates[-1]]

    def px_ago(n):
        target = (datetime.now() - timedelta(days=n)).strftime("%Y-%m-%d")
        past = [d for d in dates if d <= target]
        return by_date[past[-1]] if past else None

    px_7  = px_ago(7)
    pct_7d = ((today_px - px_7) / px_7 * 100) if px_7 else None

    avg_7d  = round(sum(by_date[d] for d in dates[-7:])  / min(len(dates), 7),  0)
    avg_14d = round(sum(by_date[d] for d in dates[-14:]) / min(len(dates), 14), 0)

    if   pct_7d is None:  direction = "unknown"
    elif pct_7d < -2:     direction = "falling"
    elif pct_7d > 2:      direction = "rising"
    else:                 direction = "stable"

    # Consecutive days in same direction
    consecutive = 0
    prev = None
    for d in reversed(dates):
        p = by_date[d]
        if prev is None:
            prev = p; continue
        if direction == "falling" and p <= prev: consecutive += 1
        elif direction == "rising" and p >= prev: consecutive += 1
        elif direction == "stable" and abs(p - prev) / max(prev, 1) <= 0.02: consecutive += 1
        else: break
        prev = p

    below_avg = avg_14d and today_px < avg_14d
    pct_str   = f"{pct_7d:+.1f}%" if pct_7d else ""

    if direction == "falling":
        signal = (f"📉 Falling {consecutive} days in a row — consider waiting"
                  if consecutive >= 3 else
                  f"📉 Trending down ({pct_str} this week)")
    elif direction == "rising":
        signal = (f"🚀 Rising {consecutive} days straight — consider booking soon!"
                  if consecutive >= 3 else
                  f"📈 Trending up ({pct_str} this week)")
    elif below_avg:
        signal = f"🎯 Below 14-day avg by {avg_14d - today_px:.0f} {currency} — decent time to buy"
    else:
        signal = "➡️ Prices stable — no urgency"

    return {
        "direction": direction, "signal": signal,
        "pct_7d": round(pct_7d, 1) if pct_7d else None,
        "avg_7d": avg_7d, "avg_14d": avg_14d, "consecutive": consecutive,
    }


# ─────────────────────────────────────────────────────────────
# NOTIFICATION DECISION LOGIC
# ─────────────────────────────────────────────────────────────
def should_price_alert(route: dict, best_price: float, history: dict) -> bool:
    notif = route.get("notifications", {})
    if not notif.get("price_alert", {}).get("enabled", True):
        return False
    threshold = route.get("max_price_alert")
    if not threshold or best_price > threshold:
        return False
    rid_hist = history.get(route["id"], {})
    if rid_hist.get("last_alert_date") != _today():
        return True  # no alert sent today yet
    last_price = rid_hist.get("last_alert_price")
    return last_price is not None and best_price < last_price


def should_daily(route: dict, best_price: float, alert_sent: bool, history: dict) -> bool:
    if alert_sent:
        return False
    notif = route.get("notifications", {})
    if not notif.get("daily", {}).get("enabled", True):
        return False
    threshold = route.get("max_price_alert")
    if threshold and best_price > threshold:
        return False
    rid_hist = history.get(route["id"], {})
    if rid_hist.get("last_daily_date") != _today():
        return True  # no daily sent today yet
    last_price = rid_hist.get("last_daily_price")
    return last_price is not None and best_price < last_price


def should_weekly(route: dict, history: dict) -> bool:
    notif  = route.get("notifications", {})
    weekly = notif.get("weekly_summary", {})
    if not weekly.get("enabled", False):
        return False
    today_name = DAYS_OF_WEEK[datetime.now().weekday()]
    if weekly.get("day", "sunday").lower() != today_name:
        return False
    last = history.get(route["id"], {}).get("last_weekly_summary")
    return not last or (datetime.now() - datetime.strptime(last, "%Y-%m-%d")).days >= 6


# ─────────────────────────────────────────────────────────────
# MESSAGE FORMATTING
# ─────────────────────────────────────────────────────────────
def _tc(direction: str) -> str:
    return {"falling": "#27ae60", "rising": "#e74c3c",
            "stable": "#2980b9", "unknown": "#95a5a6"}[direction]


def _api_health_line(api_health: dict | None) -> str:
    """One-line raw-API health summary for the weekly heartbeat — counts are
    taken *before* client-side filtering, so a drop to near-zero here means
    the API itself is going quiet, not that our filters got stricter."""
    if not api_health or not api_health["pairs_searched"]:
        return ""
    h   = api_health
    pct = h["pairs_with_results"] / h["pairs_searched"] * 100
    return (f"🔧 API health ({h['days']}d): {h['pairs_with_results']}/{h['pairs_searched']} "
            f"searches returned raw results ({pct:.0f}%) · "
            f"avg {h['avg_per_search']:.0f} raw flights/search")


def format_message(route: dict, flights: list, trend: dict,
                   trigger: str, currency: str, passengers: int,
                   dashboard_url: str = "", api_health: dict | None = None) -> dict:
    best      = flights[0]
    top5      = flights[:5]
    label     = route.get("label", route_endpoint_label(route))
    pax_note  = f" (×{passengers} passengers)" if passengers > 1 else ""
    is_alert  = trigger == "price_alert"
    is_weekly = trigger == "weekly"
    threshold = route.get("max_price_alert")

    # ── Plain text (WhatsApp markdown: *bold* _italic_) ──────
    header = [
        f"{'🚨 ' if is_alert else ''}✈️ *{label}*",
        f"🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    if is_alert and threshold:
        header += ["", "─" * 28,
                   f"🚨 *PRICE ALERT!* {best['price']:.0f} {currency} — below target of {threshold:.0f} {currency}!",
                   "─" * 28]
    if is_weekly:
        header += ["", "─── *WEEKLY SUMMARY* ───"]

    ret_dur_line = f"   ↩️ Return:   {best.get('return_duration_str','?')}" if best.get("return_duration_str") else ""
    dw = route.get("departure_window", {})
    eff_line = (f"   ⏱ Eff. cost: *{best['effective_cost']:.0f} {currency}* (price + time + vacation)"
                if best.get("effective_cost") is not None and best["effective_cost"] != best["price"] else "")
    best_block = [
        "",
        f"💰 Best price: *{best['price']:.0f} {currency}*{pax_note}",
        eff_line,
        f"   🗓 {best['outbound_date']} {best.get('dep_time','')} → {best['return_date']} ({best['nights']} nights)",
        f"   ✈️ Outbound: {best.get('outbound_duration_str','?')}",
        ret_dur_line,
        f"   🛫 {best['airline']}  |  {best['stops']} stop(s)  |  Max layover: {best['max_layover_h']}h",
        f"   {'⭐ Within preferred time window' if best.get('preferred_time') else ('⏰ Outside preferred window' if dw.get('enabled') else '')}",
    ]

    trend_block = ["", f"📊 Trend: _{trend['signal']}_"]
    if trend["avg_7d"]:
        trend_block.append(f"   7d avg: {trend['avg_7d']:.0f}  |  14d avg: {trend['avg_14d']:.0f} {currency}")

    health_line  = _api_health_line(api_health) if is_weekly else ""
    health_block = ["", health_line] if health_line else []

    top5_block = ["", "── *Top 5* ───"]
    for i, f in enumerate(top5, 1):
        star    = "⭐" if f.get("preferred_time") else ""
        al_star = "🏷️" if f.get("preferred_airline_match") and route.get("preferred_airlines") else ""
        self_tr = "⚠️ST" if f.get("self_transfer") else ""
        flags   = " ".join(x for x in (star, al_star, self_tr) if x)
        ret_dur  = f" / ✈️back {f['return_duration_str']}" if f.get("return_duration_str") else ""
        eff_note = (f" | ⏱eff.{f['effective_cost']:.0f}"
                    if f.get("effective_cost") is not None and f["effective_cost"] != f["price"] else "")
        top5_block.append(
            f"  {i}. *{f['price']:.0f} {currency}*{' ' + flags if flags else ''}{eff_note}\n"
            f"     {f['outbound_date']} {f.get('dep_time','')} → {f['return_date']} ({f['nights']}n) | "
            f"{f['airline']} | {f['stops']} stop(s) | "
            f"✈️out {f['outbound_duration_str']}{ret_dur}"
        )

    dashboard_block = []
    if dashboard_url:
        anchor = f"{dashboard_url.rstrip('/')}#route-{route['id']}"
        dashboard_block = ["", f"📊 Full details & chart: {anchor}"]

    plain = "\n".join(header + best_block + trend_block + health_block + top5_block + dashboard_block)

    # WhatsApp: compact variant — no Trend/Top 5 (too long, pushes the
    # dashboard link past CallMeBot's message-length cutoff), link stays visible
    whatsapp_text = "\n".join(header + best_block + health_block + dashboard_block)

    # ── Subject ──────────────────────────────────────────────
    prefix = "🚨 PRICE ALERT! " if is_alert else ("📅 Weekly: " if is_weekly else "✈️ ")
    subject = (f"{prefix}{label}: {best['price']:.0f} {currency} "
               f"({best['nights']}n) — {datetime.now().strftime('%b %d')}")

    # ── HTML (email) ─────────────────────────────────────────
    alert_html = ""
    if is_alert and threshold:
        alert_html = f"""
        <div style="background:#fff3cd;border:2px solid #e74c3c;padding:16px;margin:16px 0;
                    border-radius:8px;text-align:center">
          <h2 style="margin:0 0 6px;color:#c0392b">🚨 PRICE ALERT!</h2>
          <p style="font-size:18px;margin:0">
            <strong>{best['price']:.0f} {currency}</strong> is below your target of
            <strong>{threshold:.0f} {currency}</strong>
          </p>
        </div>"""

    rows = ""
    for i, f in enumerate(top5, 1):
        bg    = "#e8f8f0" if f.get("preferred_time") else ("#f9f9f9" if i % 2 else "#fff")
        flags = ("⭐" if f.get("preferred_time") else "") + \
                (" 🏷️" if f.get("preferred_airline_match") and route.get("preferred_airlines") else "") + \
                (" ⚠️ST" if f.get("self_transfer") else "")
        eff_td = ""
        if f.get("effective_cost") is not None and f["effective_cost"] != f["price"]:
            eff_td = (f"<td style='padding:6px 10px;color:#6c3483;white-space:nowrap'>"
                      f"⏱ {f['effective_cost']:.0f}</td>")
        rows += (
            f"<tr style='background:{bg}'>"
            f"<td style='padding:6px 10px'>{i}</td>"
            f"<td style='padding:6px 10px;font-weight:bold'>{f['price']:.0f} {currency}</td>"
            f"{eff_td}"
            f"<td style='padding:6px 10px'>{f['outbound_date']} <b>{f.get('dep_time','')}</b></td>"
            f"<td style='padding:6px 10px'>{f['return_date']}</td>"
            f"<td style='padding:6px 10px'>{f['nights']}n</td>"
            f"<td style='padding:6px 10px'>{f['airline']}</td>"
            f"<td style='padding:6px 10px'>{f['stops']} stop(s)</td>"
            f"<td style='padding:6px 10px'>{f['duration_str']}</td>"
            f"<td style='padding:6px 10px'>{f['max_layover_h']}h</td>"
            f"<td style='padding:6px 10px;text-align:center;white-space:nowrap'>{flags}</td>"
            f"</tr>"
        )

    tc = _tc(trend["direction"])
    avgs = (f" | 7d avg: {trend['avg_7d']:.0f}  14d avg: {trend['avg_14d']:.0f} {currency}"
            if trend.get("avg_7d") else "")

    health_html = ""
    if is_weekly and health_line:
        health_html = f'<p style="color:#7d3c98;font-size:13px;margin:4px 0 0">{health_line}</p>'

    dashboard_link_html = ""
    if dashboard_url:
        anchor = f"{dashboard_url.rstrip('/')}#route-{route['id']}"
        dashboard_link_html = f"""
        <p style="text-align:center;margin:18px 0">
          <a href="{anchor}" style="background:#1a5276;color:#fff;text-decoration:none;
                    padding:10px 22px;border-radius:6px;font-size:14px;display:inline-block">
            📊 View full dashboard &amp; price chart
          </a>
        </p>"""

    html = f"""<!DOCTYPE html><html><body
      style="font-family:Arial,sans-serif;max-width:860px;margin:auto;padding:20px">
    <h2 style="color:#1a5276">✈️ {label}</h2>
    <p style="color:#999;font-size:12px">{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
    {alert_html}
    {'<h3 style="color:#7d3c98;border-bottom:2px solid #7d3c98;padding-bottom:6px">📅 Weekly Summary</h3>' if is_weekly else ''}
    <div style="background:#eaf4fb;border-left:4px solid #2e86c1;padding:16px;border-radius:4px;margin:12px 0">
      <h3 style="margin:0 0 8px;color:#1a5276">
        💰 Best price: {best['price']:.0f} {currency}{pax_note}
      </h3>
      <p style="margin:4px 0">🗓 <b>{best['outbound_date']}</b> {best.get('dep_time','')} →
         <b>{best['return_date']}</b> ({best['nights']} nights)</p>
      <p style="margin:4px 0">✈️ {best['airline']} | {best['stops']} stop(s) | {best['duration_str']} | Max layover: {best['max_layover_h']}h</p>
      {'<p style="margin:4px 0">⭐ Within preferred time window</p>' if best.get('preferred_time') else ''}
    </div>
    <p style="color:{tc};font-weight:bold">📊 {trend['signal']}{avgs}</p>
    {health_html}
    <h3 style="margin-top:16px">📋 Top 5 Options</h3>
    <table style="border-collapse:collapse;width:100%;font-size:13px">
      <thead><tr style="background:#1a5276;color:white">
        <th style="padding:7px">#</th><th>Price</th>
        {'<th style="padding:7px;color:#d2b4de">Eff. Cost</th>' if top5 and top5[0].get("effective_cost") is not None and top5[0]["effective_cost"] != top5[0]["price"] else ''}
        <th>Depart</th><th>Return</th>
        <th>Nights</th><th>Airline</th><th>Stops</th><th>Duration</th>
        <th>Max Lay</th><th>Flags</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    {dashboard_link_html}
    <p style="color:#aaa;font-size:11px;margin-top:16px">
      ⭐ preferred time window &nbsp;·&nbsp; 🏷️ preferred airline &nbsp;·&nbsp; ⚠️ST self-transfer
      | Powered by Flight Tracker 🤖 + fli (Google Flights)
    </p></body></html>"""

    return {"subject": subject, "plain": plain, "whatsapp": whatsapp_text, "html": html}


def format_heartbeat_message(route: dict, currency: str, api_health: dict | None,
                             dashboard_url: str = "") -> dict:
    """Weekly heartbeat sent when *no* flights matched this week's searches.

    Without this, a route whose filters/dates stop matching anything would
    go completely silent — indistinguishable from the job itself being
    broken. This confirms the job ran and reports raw API health, so a
    drop in raw (pre-filter) results points at the API rather than your
    filters being too strict.
    """
    label   = route.get("label", route_endpoint_label(route))
    health_line = _api_health_line(api_health)

    header = [
        f"✈️ *{label}*",
        f"🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
        "", "─── *WEEKLY SUMMARY* ───",
        "", "📭 No flights matched your filters this week — "
            "job ran fine, just nothing to report.",
    ]
    if health_line:
        header += ["", health_line]

    dashboard_block = []
    if dashboard_url:
        anchor = f"{dashboard_url.rstrip('/')}#route-{route['id']}"
        dashboard_block = ["", f"📊 Full details & chart: {anchor}"]

    plain = "\n".join(header + dashboard_block)
    subject = f"📅 Weekly: {label} — no matches this week ({datetime.now().strftime('%b %d')})"

    health_html = (f'<p style="color:#7d3c98;font-size:13px;margin:4px 0 0">{health_line}</p>'
                   if health_line else "")
    dashboard_link_html = ""
    if dashboard_url:
        anchor = f"{dashboard_url.rstrip('/')}#route-{route['id']}"
        dashboard_link_html = f"""
        <p style="text-align:center;margin:18px 0">
          <a href="{anchor}" style="background:#1a5276;color:#fff;text-decoration:none;
                    padding:10px 22px;border-radius:6px;font-size:14px;display:inline-block">
            📊 View full dashboard &amp; price chart
          </a>
        </p>"""

    html = f"""<!DOCTYPE html><html><body
      style="font-family:Arial,sans-serif;max-width:860px;margin:auto;padding:20px">
    <h2 style="color:#1a5276">✈️ {label}</h2>
    <p style="color:#999;font-size:12px">{datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</p>
    <h3 style="color:#7d3c98;border-bottom:2px solid #7d3c98;padding-bottom:6px">📅 Weekly Summary</h3>
    <p>📭 No flights matched your filters this week — job ran fine, just nothing to report.</p>
    {health_html}
    {dashboard_link_html}
    <p style="color:#aaa;font-size:11px;margin-top:16px">Powered by Flight Tracker 🤖 + fli (Google Flights)</p>
    </body></html>"""

    return {"subject": subject, "plain": plain, "whatsapp": plain, "html": html}


# ─────────────────────────────────────────────────────────────
# NOTIFICATION CHANNELS
# ─────────────────────────────────────────────────────────────
def _route_channels(route_notif: dict, trigger: str) -> dict:
    """Resolve which channel/recipient config applies for this dispatch.

    `weekly_summary` can carry its own `channels` override — e.g. so the
    weekly digest goes to a smaller audience than daily updates/alerts
    (`{"whatsapp": ["cristian"]}` while the route's default `channels.whatsapp`
    is `true`/everyone). Falls back to the route's default `channels` when no
    trigger-specific override is set, and for all other trigger types.
    """
    if trigger == "weekly":
        override = route_notif.get("weekly_summary", {}).get("channels")
        if override is not None:
            return override
    return route_notif.get("channels", {})


def _active_channels(global_notif: dict, channels: dict) -> dict:
    return {
        "email": global_notif.get("email", {}).get("enabled", False) and channels.get("email", True),
        "ntfy":  global_notif.get("ntfy",  {}).get("enabled", False) and channels.get("ntfy",  True),
    }


def _whatsapp_recipient_list(wc: dict) -> list[dict]:
    """Normalize notifications.whatsapp into a recipient list — supports both
    the current {recipients: [...]} shape and the old flat {phone, api_key}
    single-recipient shape (kept for backward compatibility)."""
    recipients = wc.get("recipients")
    if recipients is not None:
        return recipients
    if wc.get("phone") and wc.get("api_key"):
        return [{"name": "default", "phone": wc["phone"], "api_key": wc["api_key"]}]
    return []


def _whatsapp_recipients(global_notif: dict, channels: dict) -> list[dict]:
    """Resolve which (phone, api_key) recipients to WhatsApp for this dispatch.

    Each CallMeBot recipient must individually opt in and gets their own
    api_key — there's no broadcast endpoint, so we send one request per
    person. `notifications.whatsapp.recipients` lists everyone available
    globally; the resolved `channels` dict (route default, or a trigger-
    specific override — see `_route_channels`) picks the subset via
    `channels.whatsapp`: `true` (everyone), `false` (no one), or a list of
    recipient names.
    """
    wc = global_notif.get("whatsapp", {})
    if not wc.get("enabled", False):
        return []

    recipients = _whatsapp_recipient_list(wc)

    sel = channels.get("whatsapp", True)
    if sel is True:
        return recipients
    if sel is False:
        return []
    names = {n.lower() for n in sel}
    return [r for r in recipients if r.get("name", "").lower() in names]


def send_email(content: dict, ec: dict):
    msg = MIMEMultipart("alternative")
    msg["Subject"] = content["subject"]
    msg["From"]    = ec["from_address"]
    msg["To"]      = ec["to_address"]
    msg.attach(MIMEText(content["plain"], "plain"))
    msg.attach(MIMEText(content["html"],  "html"))
    try:
        with smtplib.SMTP(ec["smtp_server"], ec["smtp_port"]) as s:
            s.ehlo(); s.starttls()
            s.login(ec["username"], ec["password"])
            s.sendmail(ec["from_address"], ec["to_address"], msg.as_string())
        log.info("Email sent")
    except Exception as e:
        log.error(f"Email failed: {e}")


def send_whatsapp(content: dict, wc: dict):
    who = wc.get("name", wc.get("phone", "?"))
    text = content.get("whatsapp", content["plain"])
    if len(text) > 900:
        text = text[:880] + "\n[…]"
    url = (f"https://api.callmebot.com/whatsapp.php"
           f"?phone={urllib.parse.quote(wc['phone'])}"
           f"&text={urllib.parse.quote(text)}"
           f"&apikey={wc['api_key']}")
    try:
        r = requests.get(url, timeout=20)
        log.info(f"WhatsApp sent to {who}" if r.status_code == 200 else
                 f"WhatsApp to {who} failed: {r.status_code}")
    except Exception as e:
        log.error(f"WhatsApp to {who} error: {e}")


def send_ntfy(content: dict, nc: dict, route_id: str, channels: dict):
    topic    = channels.get("ntfy_topic") or nc.get("topic", "flight-tracker")
    server   = nc.get("server", "https://ntfy.sh")
    priority = "urgent" if any(k in content["subject"] for k in ("ALERT", "FAILED")) else "default"
    # HTTP headers must be ASCII — strip emoji from the title
    title    = content["subject"].encode("ascii", errors="ignore").decode("ascii").strip()
    try:
        requests.post(
            f"{server}/{topic}",
            data=content["plain"][:500].encode("utf-8"),
            headers={
                "Title":    title,
                "Priority": priority,
                "Tags":     "airplane,money_with_wings",
            },
            timeout=15,
        ).raise_for_status()
        log.info("ntfy sent")
    except Exception as e:
        log.error(f"ntfy error: {e}")


def dispatch(content: dict, cfg: dict, route: dict, trigger: str = "daily"):
    """Send `content` through the route's configured channels.

    `trigger` ("daily" | "price_alert" | "weekly") selects which channel
    config applies — `weekly` can use a `weekly_summary.channels` override
    to reach a smaller/different audience than the route's default
    `channels` (e.g. send daily updates to everyone but the weekly digest
    to just yourself). See `_route_channels`.
    """
    gn       = cfg.get("notifications", {})
    rn       = route.get("notifications", {})
    channels = _route_channels(rn, trigger)
    ch       = _active_channels(gn, channels)
    if ch["email"]: send_email(content, gn.get("email", {}))
    for recipient in _whatsapp_recipients(gn, channels):
        send_whatsapp(content, recipient)
    if ch["ntfy"]:  send_ntfy(content, gn.get("ntfy", {}), route["id"], channels)


# ─────────────────────────────────────────────────────────────
# ERROR REPORTS  (separate opt-in recipient list — not route-scoped)
# ─────────────────────────────────────────────────────────────
def _error_report_targets(global_notif: dict) -> dict:
    """Resolve who gets the end-of-run error summary.

    Opt-in via `receive_error_report` on each channel/recipient — kept as
    the *same* property name across email/ntfy/whatsapp (even though only
    whatsapp is multi-recipient today) so email/ntfy can grow multi-recipient
    later without a rename. Errors aren't tied to a route, so this is
    resolved straight from the global config rather than the per-route
    `channels` routing used by `dispatch()`.
    """
    ec = global_notif.get("email", {})
    nc = global_notif.get("ntfy", {})
    wc = global_notif.get("whatsapp", {})
    return {
        "email": ec.get("enabled", False) and ec.get("receive_error_report", False),
        "ntfy":  nc.get("enabled", False) and nc.get("receive_error_report", False),
        "whatsapp": [r for r in _whatsapp_recipient_list(wc) if r.get("receive_error_report", False)]
                    if wc.get("enabled", False) else [],
    }


def format_error_report(errors: list[tuple[str, str]]) -> dict:
    when    = datetime.utcnow().strftime("%Y-%m-%d %H:%M UTC")
    count   = len(errors)
    lines   = [f"🔴 *Flight Tracker — {count} route{'s' if count != 1 else ''} FAILED*",
               f"🕐 {when}", ""]
    for rid, err in errors:
        lines.append(f"• *{rid}*: {err}")
    lines.append("")
    lines.append("Other enabled routes (if any) ran normally — check the Actions log for full tracebacks.")
    plain   = "\n".join(lines)
    subject = f"🔴 Flight Tracker — {count} route{'s' if count != 1 else ''} FAILED ({datetime.now().strftime('%b %d %H:%M')})"

    items = "".join(f"<li><b>{rid}</b>: {err}</li>" for rid, err in errors)
    html = f"""<!DOCTYPE html><html><body
      style="font-family:Arial,sans-serif;max-width:860px;margin:auto;padding:20px">
    <h2 style="color:#c0392b">🔴 Flight Tracker — {count} route{'s' if count != 1 else ''} FAILED</h2>
    <p style="color:#999;font-size:12px">{when}</p>
    <ul style="font-size:14px;line-height:1.6">{items}</ul>
    <p style="color:#555">Other enabled routes (if any) ran normally — check the Actions log for full tracebacks.</p>
    </body></html>"""

    return {"subject": subject, "plain": plain, "whatsapp": plain, "html": html}


def dispatch_error_report(errors: list[tuple[str, str]], cfg: dict):
    if not errors:
        return
    gn      = cfg.get("notifications", {})
    targets = _error_report_targets(gn)
    if not (targets["email"] or targets["ntfy"] or targets["whatsapp"]):
        log.info(f"⚠️  {len(errors)} route(s) failed this run, but no error-report recipients configured")
        return

    content = format_error_report(errors)
    log.info(f"📮 Sending error report for {len(errors)} failed route(s)")
    if targets["email"]:
        send_email(content, gn.get("email", {}))
    if targets["ntfy"]:
        send_ntfy(content, gn.get("ntfy", {}), "", {})
    for recipient in targets["whatsapp"]:
        send_whatsapp(content, recipient)


def _time_offset(dt: datetime, ref: datetime) -> str:
    """Return HH:MM for dt, appending +N if dt's date is N days after ref's date."""
    if dt is None:
        return ""
    t    = dt.strftime("%H:%M")
    diff = (dt.date() - ref.date()).days
    return t + (f"+{diff}" if diff > 0 else "")


def _extract_legs(legs: list, layovers: list) -> list:
    """Convert a list of fli FlightLeg/Layover models into display-ready dicts."""
    if not legs:
        return []
    ref = legs[0].departure_datetime
    result = []
    for i, leg in enumerate(legs):
        entry = {
            "dep_code":     leg.departure_airport.name,
            "dep_name":     leg.departure_airport_name or leg.departure_airport.value,
            "dep_time":     _time_offset(leg.departure_datetime, ref),
            "arr_code":     leg.arrival_airport.name,
            "arr_name":     leg.arrival_airport_name or leg.arrival_airport.value,
            "arr_time":     _time_offset(leg.arrival_datetime, ref),
            "airline":      _fix_airline_name(leg.airline.value),
            "flight_num":   f"{leg.airline.name}{leg.flight_number}",
            "aircraft":     leg.aircraft or "",
            "duration_str": _minutes_to_hm(leg.duration),
        }
        # Layover after this leg
        if i < len(layovers):
            lay = layovers[i]
            entry["layover_after"] = {
                "code":         lay.airport.name,
                "name":         lay.airport_name or lay.airport.value,
                "duration_str": _minutes_to_hm(lay.duration),
                "overnight":    lay.overnight,
            }
        result.append(entry)
    return result


def _group_flights(flights: list, max_per_group: int = 3) -> list[dict]:
    """
    Group flights by airline combination.
    Keep the cheapest max_per_group per group.
    Sort groups by their cheapest flight.
    Returns list of {key, label, flights} dicts.
    """
    groups: dict[str, dict] = {}
    for f in flights:
        key = "+".join(sorted(f.get("airline_codes", []))) or f.get("airline", "Unknown")
        if key not in groups:
            groups[key] = {
                "key":     key,
                "label":   f.get("airline", "Unknown"),
                "flights": [],
            }
        if len(groups[key]["flights"]) < max_per_group:
            groups[key]["flights"].append(f)

    return sorted(groups.values(), key=lambda g: g["flights"][0].get("effective_cost", g["flights"][0]["price"]))


def _render_airline_group(group: dict, currency: str, preferred_airlines: list,
                           origin: str, dest: str) -> str:
    """Render one airline group as a collapsible section containing flight options."""
    flights   = group["flights"]
    label     = group["label"]
    best      = flights[0]["price"]
    count     = len(flights)
    has_pref  = any(f.get("preferred_airline_match") for f in flights)
    css       = " ag-pref" if has_pref else ""

    inner = "".join(
        _render_option(f, i, currency, preferred_airlines, origin, dest)
        for i, f in enumerate(flights, 1)
    )

    return f"""<details class='ag{css}'>
    <summary class='ag-sum'>
      <span class='ag-name'>{label}</span>
      <span class='ag-cnt'>{count} option{'s' if count != 1 else ''}</span>
      <span class='ag-best'>from {best:.0f} {currency}</span>
      {'<span style="margin-left:4px">🏷️</span>' if has_pref else ''}
    </summary>
    <div class='ag-body'>{inner}</div>
  </details>"""



def _render_direction(legs: list, dur_str: str,
                      is_outbound: bool) -> str:
    """Render one flight direction as a collapsible <details> block."""
    label   = "✈️ Outbound" if is_outbound else "↩️ Return"
    css_cls = "outbound" if is_outbound else "return-dir"
    stops   = max(0, len(legs) - 1)
    stops_s = f"{stops} stop{'s' if stops != 1 else ''}"

    legs_html = ""
    for leg in legs:
        lay = leg.get("layover_after")
        lay_html = ""
        if lay:
            night = " overnight" if lay.get("overnight") else ""
            night_flag = " 🌙 Overnight" if lay.get("overnight") else ""
            lay_html = (f"<div class='layover{night}'>⏱ Layover "
                        f"<b>{lay['code']}</b> ({lay['name']}) "
                        f"· {lay['duration_str']}{night_flag}</div>")
        legs_html += f"""
        <div class='leg'>
          <div class='leg-ap'>
            <span class='ap'><b>{leg['dep_code']}</b><span class='ap-name'>{leg['dep_name']}</span></span>
            <span class='lt'><b>{leg['dep_time']}</b></span>
            <span class='arr'>→</span>
            <span class='ap'><b>{leg['arr_code']}</b><span class='ap-name'>{leg['arr_name']}</span></span>
            <span class='lt'><b>{leg['arr_time']}</b></span>
          </div>
          <div class='leg-info'>{leg['airline']} · {leg['flight_num']} · {leg['aircraft']} · {leg['duration_str']}</div>
        </div>{lay_html}"""

    return f"""<details class='dir {css_cls}'>
      <summary>{label} &nbsp;·&nbsp; {dur_str} &nbsp;·&nbsp; {stops_s}</summary>
      <div class='legs'>{legs_html}</div>
    </details>"""


def _render_option(f: dict, idx: int, currency: str,
                   preferred_airlines: list, origin: str, dest: str) -> str:
    """Render one flight option as a collapsible tree row."""
    css = ""
    if f.get("preferred_airline_match") and preferred_airlines:
        css += " pref-al"
    if f.get("preferred_time"):
        css += " pref-time"

    flags = ("🏷️ " if (f.get("preferred_airline_match") and preferred_airlines) else "") + \
            ("⭐ " if f.get("preferred_time") else "") + \
            ("⚠️ " if f.get("self_transfer") else "")

    def fmt_d(d):
        try: return datetime.strptime(d, "%Y-%m-%d").strftime("%b %d")
        except: return d

    od = fmt_d(f.get("outbound_date", ""))
    rd = fmt_d(f.get("return_date",   ""))

    out_legs  = f.get("outbound_legs", [])
    ret_legs  = f.get("return_legs",  [])
    out_stops = max(0, len(out_legs) - 1)
    ret_stops = max(0, len(ret_legs) - 1)
    out_meta  = f"<span class='dmeta'>{f.get('outbound_duration_str','?')} · {out_stops} stop{'s' if out_stops!=1 else ''}</span>"
    ret_meta  = f"<span class='dmeta'>{f.get('return_duration_str','?')} · {ret_stops} stop{'s' if ret_stops!=1 else ''}</span>" if f.get("return_duration_str") else ""

    # Use the actual leg airport codes (not the route's nominal origin/dest) —
    # matters for multi-airport routes (e.g. destination ["NRT","HND"]) where
    # different options can land at different airports of the same city.
    o_dep = out_legs[0]["dep_code"]  if out_legs else origin
    o_arr = out_legs[-1]["arr_code"] if out_legs else dest
    r_dep = ret_legs[0]["dep_code"]  if ret_legs else dest
    r_arr = ret_legs[-1]["arr_code"] if ret_legs else origin

    out_str = (f"<b>{o_dep}</b> <b>{f.get('dep_time','')}</b> {od} "
               f"→ <b>{o_arr}</b> <b>{f.get('outbound_arr_time','')}</b> {out_meta}")
    ret_str = (f"<b>{r_dep}</b> <b>{f.get('return_dep_time','')}</b> {rd} "
               f"→ <b>{r_arr}</b> <b>{f.get('return_arr_time','')}</b> {ret_meta}")

    out_dir = _render_direction(f.get("outbound_legs", []),
                                f.get("outbound_duration_str", "?"), True)
    ret_dir = (_render_direction(f.get("return_legs", []),
                                 f.get("return_duration_str", "?"), False)
               if f.get("return_legs") else "")

    eff = f.get("effective_cost")
    eff_html = (f" <span class='efc' title='effective cost = price + time + vacation'>eff.&nbsp;{eff:.0f}</span>"
                if eff is not None and eff != f["price"] else "")

    return f"""<details class='opt{css}'>
    <summary>
      <span class='onum'>{idx}</span>
      <span class='oprice'>{f['price']:.0f} <span class='cur'>{currency}</span>{eff_html}</span>
      <span class='odates'>
        <span class='dout'>{out_str}</span>
        <span class='dsep'>↔</span>
        <span class='dret'>{ret_str}</span>
      </span>
      <span class='ometa'>{f.get('nights','?')}n · {f.get('airline','?')} · {f.get('stops','?')} stop(s) {flags}</span>
    </summary>
    <div class='obody'>{out_dir}{ret_dir}</div>
  </details>"""


# ─────────────────────────────────────────────────────────────
# DASHBOARD  (docs/index.html → GitHub Pages)
# ─────────────────────────────────────────────────────────────
def _route_filter_chips(route: dict) -> tuple[str, str]:
    """
    Build the always-visible summary line and the collapsed details line
    for a route's search-parameter disclosure widget.

    Returns (summary_html, details_html).
    """
    # ── Always-visible ────────────────────────────────────────────
    nights     = route.get("target_nights", 20)
    flex       = route.get("flexibility_days", 0)
    stay_str   = f"{nights - flex}–{nights + flex} nights" if flex else f"{nights} nights"

    dep_days   = route.get("departure_days", [])
    ret_days   = route.get("return_days",    [])
    dep_str    = " ".join(d[:3].capitalize() for d in dep_days) if dep_days else "any day"
    dep_label  = f"{dep_str} dep"
    ret_label  = (f" &nbsp;·&nbsp; {' '.join(d[:3].capitalize() for d in ret_days)} ret"
                  if ret_days else "")

    summary_html = (
        f'<span class="fp-stay">{stay_str}</span>'
        f' &nbsp;·&nbsp; <span class="fp-days">{dep_label}{ret_label}</span>'
    )

    # ── Collapsed details ─────────────────────────────────────────
    chips = []

    # Max stopovers
    stops = route.get("max_stopovers")
    if stops is None:
        stops_str = "any stops"
    elif stops == 0:
        stops_str = "direct only"
    elif stops == 1:
        stops_str = "max 1 stop"
    else:
        stops_str = f"max {stops} stops"
    chips.append(f'<span class="fp-chip">{stops_str}</span>')

    # Preferred airlines
    pref = _get_preferred_airlines(route)
    if pref:
        mode     = route.get("preferred_airline_mode", "soft")
        mode_tip = (
            "soft: all airlines shown, preferred ones get 🏷️"
            if mode == "soft"
            else "hard: only preferred airlines are returned"
        )
        chips.append(
            f'<span class="fp-chip">'
            f'{", ".join(pref)} preferred'
            f' <span class="fp-mode-badge" data-tooltip="{mode_tip}">{mode}</span>'
            f'</span>'
        )

    # Flight duration
    max_out = route.get("max_outbound_duration_hours")
    max_ret = route.get("max_return_duration_hours")
    if max_out or max_ret:
        parts = []
        if max_out:
            parts.append(f"≤{max_out:.0f}h out")
        if max_ret:
            parts.append(f"≤{max_ret:.0f}h ret")
        chips.append(f'<span class="fp-chip">flight duration: {" / ".join(parts)}</span>')

    # Departure window
    dw = route.get("departure_window", {})
    if dw.get("enabled"):
        dw_from = dw.get("from", "")
        dw_to   = dw.get("to",   "")
        mode     = dw.get("mode", "soft")
        mode_tip = (
            "soft: flights outside this window are shown but not highlighted"
            if mode == "soft"
            else "hard: only flights departing in this window are returned"
        )
        chips.append(
            f'<span class="fp-chip">'
            f'dep {dw_from}–{dw_to}'
            f' <span class="fp-mode-badge" data-tooltip="{mode_tip}">{mode}</span>'
            f'</span>'
        )

    # Max layover
    max_lay = route.get("max_layover_hours")
    if max_lay:
        chips.append(f'<span class="fp-chip">≤{max_lay:.0f}h layover</span>')

    # Time-value scoring
    tv = _time_value_config(route)
    if tv:
        daily_rate  = tv.get("daily_rate_eur", 200)
        work_hours  = tv.get("work_hours_per_day", 8)
        base_out    = tv.get("base_outbound_duration_hours", "?")
        base_ret    = tv.get("base_return_duration_hours", "?")
        threshold   = tv.get("departure_threshold", "18:00")
        hourly_rate = float(daily_rate) / float(work_hours)
        chips.append(
            f'<span class="fp-chip fp-chip-tv" '
            f'title="effective cost = price + extra hours × {hourly_rate:.0f}€/h + vacation day if dep before {threshold}">'
            f'⏱ time-value: {hourly_rate:.0f}€/h · base {base_out}h out / {base_ret}h ret · vac. before {threshold}'
            f'</span>'
        )

    details_html = " ".join(chips)
    return summary_html, details_html


def generate_dashboard(routes: list, history: dict) -> str:
    cards      = ""
    chart_data = ""

    for route in routes:
        rid      = route["id"]
        label    = route.get("label", route_endpoint_label(route))
        currency = route.get("currency", "EUR")
        entries  = history.get(rid, {}).get("entries", [])

        if not entries:
            price_str   = "—"
            best_ever   = "—"
            trend_sig   = "No data yet"
            tc          = "#95a5a6"
            flights_tbl = "<p style='color:#999'>No data yet.</p>"
        else:
            last      = entries[-1]
            price_str = f"{last['best_price']:.0f}"
            best_ever = f"{min(e['best_price'] for e in entries):.0f}"
            trend     = analyze_trend(entries, currency)
            trend_sig = trend["signal"]
            tc        = _tc(trend["direction"])

            top = last.get("top_flights", [])[:route.get("top_flights_count", 30)]
            preferred_airlines = route.get("preferred_airlines", [])
            max_per_airline    = route.get("max_per_airline", 3)
            groups             = _group_flights(top, max_per_group=max_per_airline)
            options_html = "".join(
                _render_airline_group(g, currency, preferred_airlines,
                                      endpoint_label(_route_first_origin(route)),
                                      endpoint_label(_route_final_destination(route)))
                for g in groups
            ) if groups else "<p style='color:#999'>No flights today.</p>"
            flights_tbl = f"<div class='options'>{options_html}</div>"

        chart_labels = [e["date"][5:] for e in entries[-30:]]
        chart_prices = [e["best_price"]  for e in entries[-30:]]
        avg14 = (analyze_trend(entries, currency).get("avg_14d")
                 if len(entries) >= 2 else None)
        chart_data += (f"{{id:'{rid}',label:{json.dumps(label)},"
                       f"labels:{json.dumps(chart_labels)},"
                       f"prices:{json.dumps(chart_prices)},"
                       f"avg14:{json.dumps(avg14)},currency:'{currency}'}},\n")

        mp = route.get("max_price_alert")
        alert_badge = ""
        if mp and entries:
            below  = entries[-1]["best_price"] <= mp
            col    = "#27ae60" if below else "#c0392b"
            txt    = f"{'✅ Below' if below else '❌ Above'} target {mp:.0f} {currency}"
            alert_badge = (f'<span style="background:{col};color:#fff;padding:2px 8px;'
                           f'border-radius:10px;font-size:11px">{txt}</span>')

        fp_summary, fp_details = _route_filter_chips(route)

        cards += f"""
        <div class="card" id="route-{rid}">
          <div class="card-head">
            <h3>{label}</h3>
            <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:4px">
              <span class="badge">{route_endpoint_label(route)}</span>
              {alert_badge}
            </div>
            <details class="fparams">
              <summary>{fp_summary}</summary>
              <div class="fparams-body">{fp_details}</div>
            </details>
          </div>
          <div style="display:flex;align-items:baseline;gap:12px;margin:10px 0">
            <span style="font-size:38px;font-weight:700;color:#1a5276">{price_str}</span>
            <span style="color:#888;font-size:16px">{currency}</span>
            <span style="color:#aaa;font-size:12px">All-time best: <b>{best_ever} {currency}</b></span>
          </div>
          <div style="color:{tc};font-size:13px;font-weight:600;margin-bottom:10px">{trend_sig}</div>
          <div style="position:relative;height:140px;margin-bottom:14px">
            <canvas id="chart-{rid}"></canvas>
          </div>
          <div>
            <div style="font-size:11px;color:#888;margin-bottom:5px">
              Today's best options grouped by airline &nbsp;·&nbsp; top 3 per airline &nbsp;·&nbsp;
              <span style="color:#27ae60;font-weight:bold">■</span> preferred airline &nbsp;
              <span style="color:#2980b9;font-weight:bold">■</span> preferred time &nbsp;
              🌙 overnight layover &nbsp;· Click rows to expand
            </div>
            {flights_tbl}
          </div>
        </div>"""

    return f"""<!DOCTYPE html>
<html lang="en">
<head>
  <meta charset="UTF-8">
  <meta name="viewport" content="width=device-width,initial-scale=1">
  <title>✈️ Flight Price Tracker</title>
  <script src="https://cdn.jsdelivr.net/npm/chart.js@4.4.0/dist/chart.umd.min.js"></script>
  <style>
    *{{box-sizing:border-box;margin:0;padding:0}}
    body{{font-family:-apple-system,BlinkMacSystemFont,"Segoe UI",sans-serif;
          background:#f0f4f8;color:#333;padding:16px}}
    header{{background:linear-gradient(135deg,#1a5276,#2980b9);color:#fff;
            padding:18px 22px;border-radius:12px;margin-bottom:20px;
            display:flex;justify-content:space-between;align-items:center}}
    header h1{{font-size:20px}}
    .upd{{font-size:11px;opacity:.75}}
    .grid{{display:grid;grid-template-columns:repeat(auto-fill,minmax(500px,1fr));gap:18px}}
    .card{{background:#fff;border-radius:12px;padding:18px;
           box-shadow:0 2px 10px rgba(0,0,0,.07)}}
    .card-head{{border-bottom:1px solid #eee;padding-bottom:10px;margin-bottom:10px}}
    .card-head h3{{font-size:15px;color:#1a5276}}
    .badge{{background:#eaf4fb;color:#2e86c1;padding:2px 8px;
            border-radius:10px;font-size:11px}}
    table{{width:100%;border-collapse:collapse;font-size:11px;min-width:460px}}
    th{{background:#1a5276;color:#fff;padding:5px 7px;text-align:left;white-space:nowrap}}
    td{{padding:5px 7px;border-bottom:1px solid #f4f4f4}}
    tr:hover td{{background:#f0f8ff}}
    /* ── Airline groups ────────────────────────────── */
    .ag{{margin:8px 0;border:1px solid #d5e8f5;border-radius:10px;overflow:hidden}}
    .ag>summary{{display:flex;align-items:center;gap:10px;padding:10px 14px;
                 background:#eaf4fb;cursor:pointer}}
    .ag>summary:hover{{background:#d5e8f5}}
    .ag>summary::before{{content:'▶';font-size:10px;color:#aaa;transition:transform .2s}}
    .ag[open]>summary::before{{transform:rotate(90deg)}}
    .ag-pref>summary{{background:#eafaf1;border-left:4px solid #27ae60}}
    .ag-name{{font-weight:700;font-size:14px;color:#1a5276;flex:1}}
    .ag-cnt{{font-size:12px;color:#888}}
    .ag-best{{font-size:13px;font-weight:600;color:#27ae60}}
    .ag-body{{padding:6px 8px;background:#fff}}
    /* ── Tree view ─────────────────────────────── */
    .options{{margin-top:6px}}
    details summary{{cursor:pointer;list-style:none}}
    details summary::-webkit-details-marker{{display:none}}
    .opt{{margin:4px 0;border:1px solid #e0e0e0;border-radius:8px;overflow:hidden}}
    .opt>summary{{display:flex;align-items:center;gap:8px;padding:9px 12px;
                  background:#f8f9fa;flex-wrap:wrap}}
    .opt>summary:hover{{background:#e9ecef}}
    .opt>summary::before{{content:'▶';font-size:9px;color:#aaa;flex-shrink:0;
                          transition:transform .2s}}
    .opt[open]>summary::before{{transform:rotate(90deg)}}
    .opt.pref-al>summary{{background:#eafaf1;border-left:4px solid #27ae60}}
    .opt.pref-time>summary{{background:#eaf4fb;border-left:4px solid #2980b9}}
    .opt.pref-al.pref-time>summary{{background:#e8f8f0;border-left:4px solid #1e8449}}
    .onum{{color:#bbb;font-size:11px;min-width:16px}}
    .oprice{{font-weight:700;font-size:16px;color:#1a5276;min-width:85px}}
    .efc{{font-size:11px;font-weight:400;color:#7d3c98;margin-left:4px;white-space:nowrap}}
    .cur{{font-size:12px;font-weight:400;color:#888}}
    .odates{{flex:1;font-size:12px;line-height:1.7;min-width:260px}}
    .dout,.dret{{display:block}}
    .dsep{{display:none}}
    .dmeta{{color:#888;font-size:11px;margin-left:4px}}
    .ometa{{font-size:11px;color:#888;white-space:nowrap}}
    .obody{{padding:8px 14px 12px 26px;background:#fff;border-top:1px solid #f0f0f0}}
    .dir{{margin:4px 0;border-radius:6px;overflow:hidden;border:1px solid #eee}}
    .dir>summary{{padding:6px 10px;font-size:12px;font-weight:600;
                  background:#f4f4f4;display:flex;align-items:center;gap:6px}}
    .dir>summary::before{{content:'▶';font-size:9px;color:#aaa;
                          transition:transform .2s}}
    .dir[open]>summary::before{{transform:rotate(90deg)}}
    .dir>summary:hover{{background:#e8e8e8}}
    .outbound>summary{{border-left:3px solid #2980b9}}
    .return-dir>summary{{border-left:3px solid #27ae60}}
    .legs{{padding:6px 10px}}
    .leg{{padding:6px 0;border-bottom:1px solid #f4f4f4}}
    .leg:last-child{{border-bottom:none}}
    .leg-ap{{display:flex;align-items:center;gap:6px;font-size:12px;flex-wrap:wrap}}
    .ap{{display:inline-flex;flex-direction:column;line-height:1.2}}
    .ap-name{{font-size:10px;color:#aaa}}
    .lt{{font-size:13px;padding:0 2px}}
    .arr{{color:#ccc}}
    .leg-info{{font-size:11px;color:#999;margin-top:2px}}
    .layover{{padding:3px 8px;margin:3px 0;background:#fff8e1;
              border-radius:4px;font-size:11px;color:#856404}}
    .layover.overnight{{background:#fde8e8;color:#842029}}
    /* ── Search-parameter disclosure widget ──────────────── */
    .fparams{{margin-top:8px;font-size:11px;color:#888}}
    .fparams>summary{{list-style:none;cursor:pointer;display:inline-flex;
                      align-items:center;gap:0}}
    .fparams>summary::-webkit-details-marker{{display:none}}
    .fparams>summary::before{{content:"+ ";color:#2980b9;font-weight:700;
                              white-space:pre;font-size:12px}}
    .fparams[open]>summary::before{{content:"− ";}}
    .fparams-body{{display:flex;flex-wrap:wrap;gap:5px;margin-top:5px;padding-left:14px}}
    .fp-chip{{background:#f0f4f8;border:1px solid #dde3ea;border-radius:8px;
              padding:2px 7px;font-size:11px;color:#555}}
    .fp-chip-tv{{background:#f5eef8;border-color:#d2b4de;color:#6c3483}}
    .fp-mode-badge{{display:inline-block;background:#e8f4fc;color:#2980b9;
                    border-radius:4px;padding:0 4px;font-size:10px;
                    font-weight:600;cursor:help;position:relative}}
    .fp-mode-badge[data-tooltip]:hover::after{{
      content:attr(data-tooltip);
      position:absolute;left:50%;transform:translateX(-50%);bottom:calc(100% + 5px);
      background:#1a1a2e;color:#fff;font-size:10px;font-weight:400;
      white-space:nowrap;padding:4px 8px;border-radius:5px;
      pointer-events:none;z-index:10;line-height:1.4}}
    .fp-mode-badge[data-tooltip]:hover::before{{
      content:"";position:absolute;left:50%;transform:translateX(-50%);
      bottom:calc(100% + 1px);border:4px solid transparent;
      border-top-color:#1a1a2e;pointer-events:none;z-index:10}}
    @media(max-width:600px){{.grid{{grid-template-columns:1fr}}}}
  </style>
</head>
<body>
<header>
  <h1>✈️ Flight Price Tracker</h1>
  <span class="upd">Updated: {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}</span>
</header>
<div class="grid">{cards}</div>
<script>
const routes=[{chart_data}];
routes.forEach(r=>{{
  const ctx=document.getElementById('chart-'+r.id);
  if(!ctx||!r.prices.length)return;
  const ds=[{{label:'Best price',data:r.prices,borderColor:'#2980b9',
    backgroundColor:'rgba(41,128,185,.07)',tension:0.35,fill:true,pointRadius:3}}];
  if(r.avg14!==null)ds.push({{label:'14d avg',data:r.labels.map(()=>r.avg14),
    borderColor:'#e67e22',borderDash:[5,5],borderWidth:1.5,pointRadius:0,fill:false}});
  new Chart(ctx,{{type:'line',data:{{labels:r.labels,datasets:ds}},
    options:{{responsive:true,maintainAspectRatio:false,
      plugins:{{legend:{{display:false}}}},
      scales:{{
        y:{{ticks:{{callback:v=>v+' '+r.currency,font:{{size:10}}}},grid:{{color:'#f0f0f0'}}}},
        x:{{ticks:{{font:{{size:10}}}},grid:{{display:false}}}}}}}}}});
}});
</script>
</body></html>"""


def _select_top_flights(flights: list, limit: int, max_per_airline: int) -> list:
    """
    Pick up to `limit` cheapest flights (already price-sorted) while capping
    each airline-combination at `max_per_airline`. Without the cap, a single
    dominant carrier (e.g. lots of cheap Turkish Airlines combos) can fill
    the entire stored/displayed set, crowding out pricier airlines that would
    otherwise have a chance to surface in `top_flights` and the dashboard.
    """
    selected = []
    counts: dict[str, int] = {}
    for f in flights:
        key = "+".join(sorted(f.get("airline_codes", []))) or f.get("airline", "Unknown")
        if counts.get(key, 0) >= max_per_airline:
            continue
        selected.append(f)
        counts[key] = counts.get(key, 0) + 1
        if len(selected) >= limit:
            break
    return selected


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def process_route(route: dict, cfg: dict, history: dict) -> list[tuple[dict, str]]:
    """Search one route, update history, and return pending notifications.

    Returns a list of (content, trigger) tuples to be dispatched by the
    caller after the dashboard has been regenerated.
    """
    rid           = route["id"]
    label         = route.get("label", route_endpoint_label(route))
    currency      = route.get("currency", "EUR")
    passengers    = route.get("passengers", 1)
    dashboard_url = cfg.get("dashboard_url", "")
    pending: list[tuple[dict, str]] = []

    log.info(f"\n{'═' * 60}")
    log.info(f"Route: {label}")
    log.info(f"{'═' * 60}")
    log.info(f"Searching {label} …")
    flights, api_stats = search_route(route)
    add_api_stats(history, rid, label, api_stats)

    if not flights:
        log.warning(f"[{rid}] No flights found — skipping")
        if should_weekly(route, history):
            log.info(f"[{rid}] 📅 Weekly heartbeat (no matches this week)")
            health = api_health_summary(history, rid)
            msg = format_heartbeat_message(route, currency, health, dashboard_url)
            pending.append((msg, "weekly"))
            history[rid]["last_weekly_summary"] = _today()
        return pending

    best_price = flights[0]["price"]
    log.info(f"[{rid}] Best price today: {best_price:.0f} {currency} "
             f"({flights[0]['outbound_date']} → {flights[0]['return_date']}, "
             f"{flights[0]['nights']}n, {flights[0]['airline']})")

    top_flights_count = route.get("top_flights_count", 30)
    max_per_airline   = route.get("max_per_airline", 3)
    top_flights       = _select_top_flights(flights, top_flights_count, max_per_airline)

    entry = {
        "date":         _today(),
        "best_price":   best_price,
        "currency":     currency,
        "outbound_date": flights[0]["outbound_date"],
        "dep_time":     flights[0].get("dep_time", ""),
        "return_date":  flights[0]["return_date"],
        "nights":       flights[0]["nights"],
        "airline":      flights[0]["airline"],
        "stops":        flights[0]["stops"],
        "duration_str": flights[0]["duration_str"],
        "top_flights":  [{k: v for k, v in f.items() if k != "raw"}
                         for f in top_flights],
    }
    add_entry(history, rid, label, entry)

    entries = recent_entries(history, rid, days=30)
    trend   = analyze_trend(entries, currency)

    alert_sent = False

    if should_price_alert(route, best_price, history):
        log.info(f"[{rid}] 🚨 Price alert: {best_price:.0f} {currency}")
        msg = format_message(route, flights, trend, "price_alert", currency, passengers, dashboard_url)
        pending.append((msg, "price_alert"))
        history[rid]["last_alert_date"]  = _today()
        history[rid]["last_alert_price"] = best_price
        alert_sent = True

    if should_daily(route, best_price, alert_sent, history):
        log.info(f"[{rid}] 📅 Daily digest")
        msg = format_message(route, flights, trend, "daily", currency, passengers, dashboard_url)
        pending.append((msg, "daily"))
        history[rid]["last_daily_date"]  = _today()
        history[rid]["last_daily_price"] = best_price

    if should_weekly(route, history):
        log.info(f"[{rid}] 📅 Weekly summary")
        health = api_health_summary(history, rid)
        msg = format_message(route, flights, trend, "weekly", currency, passengers, dashboard_url, api_health=health)
        pending.append((msg, "weekly"))
        history[rid]["last_weekly_summary"] = _today()

    return pending


def run(debug: bool = False, only_route: str = ""):
    global DEBUG
    DEBUG = debug
    if DEBUG:
        log.info("🔍 DEBUG MODE — raw API responses saved to debug/ folder")

    cfg = None
    try:
        cfg     = load_config()
        history = load_history()

        route_errors: list[tuple[str, str]] = []
        # Collect (route, content, trigger) tuples from all routes; dispatch
        # after the dashboard is written so notifications and the live page
        # are always in sync.
        pending_notifications: list[tuple[dict, dict, str]] = []
        pending_lock = threading.Lock()
        errors_lock  = threading.Lock()

        active_routes = []
        for route in cfg.get("routes", []):
            if only_route and route["id"] != only_route:
                continue
            if not route.get("enabled", True):
                log.info(f"⏸️  Skipping disabled route: {route.get('label', route['id'])}")
                continue
            active_routes.append(route)

        def _run_route(route: dict) -> None:
            try:
                for content, trigger in process_route(route, cfg, history):
                    with pending_lock:
                        pending_notifications.append((route, content, trigger))
            except Exception as e:
                log.error(f"Error on route {route.get('id','?')}: {e}", exc_info=True)
                with errors_lock:
                    route_errors.append((route.get("id", "?"), str(e)))

        with ThreadPoolExecutor(max_workers=max(len(active_routes), 1)) as executor:
            list(executor.map(_run_route, active_routes))

        save_history(history)
        log.info("💾 price_history.json saved")

        DASHBOARD_DIR.mkdir(exist_ok=True)
        (DASHBOARD_DIR / ".nojekyll").touch()
        active_routes = [r for r in cfg["routes"] if r.get("enabled", True)]
        DASHBOARD_FILE.write_text(generate_dashboard(active_routes, history),
                                  encoding="utf-8")
        log.info(f"🌐 Dashboard → {DASHBOARD_FILE}")

        log.info(f"📤 Sending {len(pending_notifications)} notification(s) …")
        for route, content, trigger in pending_notifications:
            dispatch(content, cfg, route, trigger=trigger)

        dispatch_error_report(route_errors, cfg)

        log.info("✅ Done.")
    except Exception as e:
        log.error(f"💥 Flight tracker run crashed: {e}", exc_info=True)
        # cfg may not have loaded (e.g. bad FLIGHT_CONFIG secret) — only
        # attempt notification if we have somewhere to send it from. GitHub
        # Actions' own failure-email mechanism is the backstop either way,
        # since we re-raise to keep the run marked as failed.
        if cfg is not None:
            dispatch_error_report([("flight-tracker-job", f"Run crashed: {e}")], cfg)
        raise


if __name__ == "__main__":
    import argparse
    parser = argparse.ArgumentParser(description="Flight Price Tracker")
    parser.add_argument(
        "--debug", action="store_true",
        help="Save raw API responses to debug/ folder and show filter breakdown"
    )
    parser.add_argument(
        "--route", default="",
        help="Only run a specific route by ID (e.g. --route otp-akl-2027)"
    )
    args = parser.parse_args()
    run(debug=args.debug, only_route=args.route)
