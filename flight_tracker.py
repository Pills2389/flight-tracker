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
import urllib.parse
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


# ─────────────────────────────────────────────────────────────
# CONFIG
# ─────────────────────────────────────────────────────────────
def load_config() -> dict:
    env = os.environ.get("FLIGHT_CONFIG")
    if env:
        log.info("Loading config from FLIGHT_CONFIG env var")
        return json.loads(env)
    if Path("config.json").exists():
        with open("config.json", encoding="utf-8") as f:
            return json.load(f)
    log.error("No config found. Copy config.example.json → config.json")
    sys.exit(1)


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


# ─────────────────────────────────────────────────────────────
# FLI — BUILD SEARCH FILTERS
# ─────────────────────────────────────────────────────────────
def _build_filters(route: dict, departure: str, ret: str) -> FlightSearchFilters:
    """Build a typed FlightSearchFilters for one departure/return date pair."""
    origin      = Airport[route["origin"].upper()]
    destination = Airport[route["destination"].upper()]

    dw = route.get("departure_window", {})
    outbound_restrictions = None
    if dw.get("enabled") and dw.get("mode") == "hard":
        outbound_restrictions = TimeRestrictions(
            earliest_departure=int(dw["from"].split(":")[0]),
            latest_departure=int(dw["to"].split(":")[0]),
        )

    segments = [
        FlightSegment(
            departure_airport=[[origin, 0]],
            arrival_airport=[[destination, 0]],
            travel_date=departure,
            time_restrictions=outbound_restrictions,
        ),
        FlightSegment(
            departure_airport=[[destination, 0]],
            arrival_airport=[[origin, 0]],
            travel_date=ret,
        ),
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
        trip_type=TripType.ROUND_TRIP,
        passenger_info=PassengerInfo(adults=route.get("passengers", 1)),
        flight_segments=segments,
        stops=_max_stops(route.get("max_stopovers")),
        airlines=airlines,
        alliances=alliances,
        layover_restrictions=layover_restrictions,
        bags=BagsFilter(checked_bags=bags) if bags else None,
        sort_by=SortBy.CHEAPEST,
    )


# ─────────────────────────────────────────────────────────────
# FLI — RUN & PARSE
# ─────────────────────────────────────────────────────────────
def _run_search(search: SearchFlights, route: dict, departure: str, ret: str,
                top_n: int, debug_label: str = "") -> list[dict]:
    """Run one round-trip search via the fli Python API and return parsed flights.

    Google's backend occasionally returns an empty payload for a query that
    returns stable, non-empty results moments later (a transient glitch, not
    a sign the route/dates have no flights — confirmed by hand: identical
    retries return byte-identical flight data). We retry empty responses a
    few times before accepting "no results" as final.
    """
    filters = _build_filters(route, departure, ret)
    retries = route.get("search_retries", 3)

    results = None
    for attempt in range(retries + 1):
        try:
            results = search.search(
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
            log.info(f"  {departure} → {ret}: empty response, "
                     f"retrying ({attempt + 1}/{retries})…")
            time.sleep(2)

    if not results:
        return []

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

    out = []
    for outbound, return_flight in pairs:
        try:
            f = _parse_pair(outbound, return_flight)
            if f:
                out.append(f)
        except Exception as e:
            log.debug(f"Skipping unparseable flight: {e}")
    return out


def _parse_pair(outbound, return_flight) -> dict | None:
    """
    Build a normalized flight dict from a (outbound, return) FlightResult pair.

    Mirrors fli's own CLI serialization for round trips: the combined price,
    currency, duration and stop count live on the outbound leg / are summed
    across both legs (see fli/cli/utils.py `_serialize_flight_result`).
    """
    if outbound.price is None:
        return None
    price = float(outbound.price)

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
        al_name = leg.airline.value
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
        "self_transfer":          bool(outbound.self_transfer),
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


def search_route(route: dict, search: SearchFlights) -> list[dict]:
    """
    Sample departure dates across the window and return all flight
    options found, enriched with outbound/return date info.
    """
    n_samples    = route.get("daily_samples", 8)
    dep_dates    = _sample_dates(
        route["date_from"], route["date_to"], n_samples,
        departure_days=route.get("departure_days") or None
    )

    dw     = route.get("departure_window", {})
    top_n  = route.get("top_n", 20)
    results = []

    for dep in dep_dates:
        for ret in _return_dates(dep, route):
            nights = (datetime.strptime(ret, "%Y-%m-%d") -
                      datetime.strptime(dep, "%Y-%m-%d")).days
            if nights < 1:
                continue

            label   = f"{route['id']}_{dep}_{ret}"
            flights = _run_search(search, route, dep, ret, top_n,
                                  debug_label=label if DEBUG else "")

            if not flights:
                log.info(f"  {dep} → {ret}: no results")
                continue

            log.info(f"  {dep} → {ret} ({nights}n): "
                     f"{len(flights)} options, best {flights[0]['price']:.0f} {route.get('currency','EUR')}")

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
                f["origin"]        = route["origin"]
                f["destination"]   = route["destination"]

                # Duration filters (outbound and return separately)
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

    return results


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
        }
    history[rid]["entries"] = [
        e for e in history[rid]["entries"] if e["date"] != _today()
    ]
    history[rid]["entries"].append(entry)
    history[rid]["entries"] = history[rid]["entries"][-180:]


def recent_entries(history: dict, rid: str, days: int = 30) -> list:
    if rid not in history:
        return []
    cutoff = (datetime.now() - timedelta(days=days)).strftime("%Y-%m-%d")
    return [e for e in history[rid]["entries"] if e["date"] >= cutoff]


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
    return history.get(route["id"], {}).get("last_alert_date") != _today()


def should_daily(route: dict, best_price: float, alert_sent: bool) -> bool:
    if alert_sent:
        return False
    notif = route.get("notifications", {})
    if not notif.get("daily", {}).get("enabled", True):
        return False
    threshold = route.get("max_price_alert")
    return best_price <= threshold if threshold else True


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


def format_message(route: dict, flights: list, trend: dict,
                   trigger: str, currency: str, passengers: int) -> dict:
    best      = flights[0]
    top5      = flights[:5]
    label     = route.get("label", f"{route['origin']} → {route['destination']}")
    pax_note  = f" (×{passengers} passengers)" if passengers > 1 else ""
    is_alert  = trigger == "price_alert"
    is_weekly = trigger == "weekly"
    threshold = route.get("max_price_alert")

    # ── Plain text ───────────────────────────────────────────
    lines = [
        f"{'🚨 ' if is_alert else ''}✈️  {label}",
        f"🕐 {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}",
    ]
    if is_alert and threshold:
        lines += ["", "=" * 52,
                  f"🚨 PRICE ALERT! {best['price']:.0f} {currency} — below target of {threshold:.0f} {currency}!",
                  "=" * 52]
    if is_weekly:
        lines += ["", "─── WEEKLY SUMMARY ────────────────────────────"]

    ret_dur_line = f"   ↩️  Return:   {best.get('return_duration_str','?')}" if best.get("return_duration_str") else ""
    dw = route.get("departure_window", {})
    lines += [
        "",
        f"💰 Best price: {best['price']:.0f} {currency}{pax_note}",
        f"   🗓  {best['outbound_date']} {best.get('dep_time','')} → {best['return_date']} ({best['nights']} nights)",
        f"   ✈️  Outbound: {best.get('outbound_duration_str','?')}",
        ret_dur_line,
        f"   🛫 {best['airline']}  |  {best['stops']} stop(s)  |  Max layover: {best['max_layover_h']}h",
        f"   {'⭐ Within preferred time window' if best.get('preferred_time') else ('⏰ Outside preferred window' if dw.get('enabled') else '')}",
        "",
        f"📊 Trend: {trend['signal']}",
    ]
    if trend["avg_7d"]:
        lines.append(f"   7d avg: {trend['avg_7d']:.0f}  |  14d avg: {trend['avg_14d']:.0f} {currency}")

    lines += ["", "── Top 5 ──────────────────────────────────────"]
    for i, f in enumerate(top5, 1):
        star    = "⭐ " if f.get("preferred_time") else "   "
        al_star = "🏷️ " if f.get("preferred_airline_match") and route.get("preferred_airlines") else ""
        self_tr = "⚠️ST " if f.get("self_transfer") else ""
        ret_dur = f" / ✈️back {f['return_duration_str']}" if f.get("return_duration_str") else ""
        lines.append(
            f"  {i}. {star}{al_star}{self_tr}{f['price']:.0f} {currency} | "
            f"{f['outbound_date']} {f.get('dep_time','')} → {f['return_date']} ({f['nights']}n) | "
            f"{f['airline']} | {f['stops']} stop(s) | "
            f"✈️out {f['outbound_duration_str']}{ret_dur}"
        )

    plain = "\n".join(lines)

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
        bg   = "#e8f8f0" if f.get("preferred_time") else ("#f9f9f9" if i % 2 else "#fff")
        rows += (
            f"<tr style='background:{bg}'>"
            f"<td style='padding:6px 10px'>{i}</td>"
            f"<td style='padding:6px 10px;font-weight:bold'>{f['price']:.0f} {currency}</td>"
            f"<td style='padding:6px 10px'>{f['outbound_date']} <b>{f.get('dep_time','')}</b></td>"
            f"<td style='padding:6px 10px'>{f['return_date']}</td>"
            f"<td style='padding:6px 10px'>{f['nights']}n</td>"
            f"<td style='padding:6px 10px'>{f['airline']}</td>"
            f"<td style='padding:6px 10px'>{f['stops']} stop(s)</td>"
            f"<td style='padding:6px 10px'>{f['duration_str']}</td>"
            f"<td style='padding:6px 10px'>{f['max_layover_h']}h</td>"
            f"<td style='padding:6px 10px;text-align:center'>{'⭐' if f.get('preferred_time') else ''}</td>"
            f"</tr>"
        )

    tc = _tc(trend["direction"])
    avgs = (f" | 7d avg: {trend['avg_7d']:.0f}  14d avg: {trend['avg_14d']:.0f} {currency}"
            if trend.get("avg_7d") else "")

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
    <h3 style="margin-top:16px">📋 Top 5 Options</h3>
    <table style="border-collapse:collapse;width:100%;font-size:13px">
      <thead><tr style="background:#1a5276;color:white">
        <th style="padding:7px">#</th><th>Price</th><th>Depart</th><th>Return</th>
        <th>Nights</th><th>Airline</th><th>Stops</th><th>Duration</th>
        <th>Max Lay</th><th>⭐</th>
      </tr></thead>
      <tbody>{rows}</tbody>
    </table>
    <p style="color:#aaa;font-size:11px;margin-top:16px">
      ⭐ = preferred time window | Powered by Flight Tracker 🤖 + fli (Google Flights)
    </p></body></html>"""

    return {"subject": subject, "plain": plain, "html": html}


# ─────────────────────────────────────────────────────────────
# NOTIFICATION CHANNELS
# ─────────────────────────────────────────────────────────────
def _active_channels(global_notif: dict, route_notif: dict) -> dict:
    rc = route_notif.get("channels", {})
    return {
        "email":    global_notif.get("email",    {}).get("enabled", False) and rc.get("email",    True),
        "whatsapp": global_notif.get("whatsapp", {}).get("enabled", False) and rc.get("whatsapp", True),
        "ntfy":     global_notif.get("ntfy",     {}).get("enabled", False) and rc.get("ntfy",     True),
    }


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
    text = content["plain"]
    if len(text) > 900:
        text = text[:880] + "\n[…]"
    url = (f"https://api.callmebot.com/whatsapp.php"
           f"?phone={urllib.parse.quote(wc['phone'])}"
           f"&text={urllib.parse.quote(text)}"
           f"&apikey={wc['api_key']}")
    try:
        r = requests.get(url, timeout=20)
        log.info("WhatsApp sent" if r.status_code == 200 else
                 f"WhatsApp failed: {r.status_code}")
    except Exception as e:
        log.error(f"WhatsApp error: {e}")


def send_ntfy(content: dict, nc: dict, route_id: str, route_notif: dict):
    topic    = route_notif.get("ntfy_topic") or nc.get("topic", "flight-tracker")
    server   = nc.get("server", "https://ntfy.sh")
    priority = "urgent" if "ALERT" in content["subject"] else "default"
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


def dispatch(content: dict, cfg: dict, route: dict):
    gn = cfg.get("notifications", {})
    rn = route.get("notifications", {})
    ch = _active_channels(gn, rn)
    if ch["email"]:    send_email(content,    gn.get("email", {}))
    if ch["whatsapp"]: send_whatsapp(content, gn.get("whatsapp", {}))
    if ch["ntfy"]:     send_ntfy(content,     gn.get("ntfy", {}), route["id"], rn)


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
            "airline":      leg.airline.value,
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

    return sorted(groups.values(), key=lambda g: g["flights"][0]["price"])


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

    return f"""<details class='ag{css}' open>
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

    out_stops = max(0, len(f.get("outbound_legs", [])) - 1)
    ret_stops = max(0, len(f.get("return_legs",  [])) - 1)
    out_meta  = f"<span class='dmeta'>{f.get('outbound_duration_str','?')} · {out_stops} stop{'s' if out_stops!=1 else ''}</span>"
    ret_meta  = f"<span class='dmeta'>{f.get('return_duration_str','?')} · {ret_stops} stop{'s' if ret_stops!=1 else ''}</span>" if f.get("return_duration_str") else ""

    out_str = (f"<b>{origin}</b> <b>{f.get('dep_time','')}</b> {od} "
               f"→ <b>{dest}</b> <b>{f.get('outbound_arr_time','')}</b> {out_meta}")
    ret_str = (f"<b>{dest}</b> <b>{f.get('return_dep_time','')}</b> {rd} "
               f"→ <b>{origin}</b> <b>{f.get('return_arr_time','')}</b> {ret_meta}")

    out_dir = _render_direction(f.get("outbound_legs", []),
                                f.get("outbound_duration_str", "?"), True)
    ret_dir = (_render_direction(f.get("return_legs", []),
                                 f.get("return_duration_str", "?"), False)
               if f.get("return_legs") else "")

    return f"""<details class='opt{css}'>
    <summary>
      <span class='onum'>{idx}</span>
      <span class='oprice'>{f['price']:.0f} <span class='cur'>{currency}</span></span>
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
def generate_dashboard(routes: list, history: dict) -> str:
    cards      = ""
    chart_data = ""

    for route in routes:
        rid      = route["id"]
        label    = route.get("label", f"{route['origin']} → {route['destination']}")
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

            top = last.get("top_flights", [])[:30]  # enough for grouping
            preferred_airlines = route.get("preferred_airlines", [])
            max_per_airline    = route.get("max_per_airline", 3)
            groups             = _group_flights(top, max_per_group=max_per_airline)
            options_html = "".join(
                _render_airline_group(g, currency, preferred_airlines,
                                      route["origin"], route["destination"])
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

        cards += f"""
        <div class="card">
          <div class="card-head">
            <h3>{label}</h3>
            <div style="display:flex;gap:6px;flex-wrap:wrap;margin-top:4px">
              <span class="badge">{route['origin']} → {route['destination']}</span>
              {'<span class="badge">'+route.get('preferred_airline','')+' preferred</span>' if route.get('preferred_airline') else ''}
              {alert_badge}
            </div>
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


# ─────────────────────────────────────────────────────────────
# MAIN
# ─────────────────────────────────────────────────────────────
def process_route(route: dict, cfg: dict, history: dict, search: SearchFlights):
    rid        = route["id"]
    label      = route.get("label", f"{route['origin']}→{route['destination']}")
    currency   = route.get("currency", "EUR")
    passengers = route.get("passengers", 1)

    log.info(f"Searching {label} …")
    flights = search_route(route, search)

    if not flights:
        log.warning(f"[{rid}] No flights found — skipping")
        return

    best_price = flights[0]["price"]
    log.info(f"[{rid}] Best price today: {best_price:.0f} {currency} "
             f"({flights[0]['outbound_date']} → {flights[0]['return_date']}, "
             f"{flights[0]['nights']}n, {flights[0]['airline']})")

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
                         for f in flights[:30]],
    }
    add_entry(history, rid, label, entry)

    entries = recent_entries(history, rid, days=30)
    trend   = analyze_trend(entries, currency)

    alert_sent = False

    if should_price_alert(route, best_price, history):
        log.info(f"[{rid}] 🚨 Price alert: {best_price:.0f} {currency}")
        msg = format_message(route, flights, trend, "price_alert", currency, passengers)
        dispatch(msg, cfg, route)
        history[rid]["last_alert_date"] = _today()
        alert_sent = True

    if should_daily(route, best_price, alert_sent):
        log.info(f"[{rid}] 📅 Daily digest")
        msg = format_message(route, flights, trend, "daily", currency, passengers)
        dispatch(msg, cfg, route)

    if should_weekly(route, history):
        log.info(f"[{rid}] 📅 Weekly summary")
        msg = format_message(route, flights, trend, "weekly", currency, passengers)
        dispatch(msg, cfg, route)
        history[rid]["last_weekly_summary"] = _today()


def run(debug: bool = False, only_route: str = ""):
    global DEBUG
    DEBUG = debug
    if DEBUG:
        log.info("🔍 DEBUG MODE — raw API responses saved to debug/ folder")

    cfg     = load_config()
    history = load_history()
    search  = SearchFlights()

    for route in cfg.get("routes", []):
        if only_route and route["id"] != only_route:
            continue
        log.info(f"\n{'═' * 60}")
        log.info(f"Route: {route.get('label', route['id'])}")
        log.info(f"{'═' * 60}")
        try:
            process_route(route, cfg, history, search)
        except Exception as e:
            log.error(f"Error on route {route.get('id','?')}: {e}", exc_info=True)

    save_history(history)
    log.info("💾 price_history.json saved")

    DASHBOARD_DIR.mkdir(exist_ok=True)
    (DASHBOARD_DIR / ".nojekyll").touch()
    DASHBOARD_FILE.write_text(generate_dashboard(cfg["routes"], history),
                              encoding="utf-8")
    log.info(f"🌐 Dashboard → {DASHBOARD_FILE}")
    log.info("✅ Done.")


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
