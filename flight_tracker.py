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
import shutil
import smtplib
import subprocess
import sys
import urllib.parse
from datetime import datetime, timedelta
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from pathlib import Path

import requests

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
        with open("config.json") as f:
            return json.load(f)
    log.error("No config found. Copy config.example.json → config.json")
    sys.exit(1)


# ─────────────────────────────────────────────────────────────
# FLI — FIND EXECUTABLE
# ─────────────────────────────────────────────────────────────
def find_fli() -> list[str]:
    """
    Locate the fli CLI, trying multiple methods so it works on
    Windows, Linux (GitHub Actions), and Mac without extra config.
    """
    # 1. python -m fli  (most portable — works everywhere pip installed it)
    try:
        r = subprocess.run(
            [sys.executable, "-m", "fli", "--help"],
            capture_output=True, timeout=15,
        )
        if r.returncode == 0:
            return [sys.executable, "-m", "fli"]
    except Exception:
        pass

    # 2. fli in PATH
    p = shutil.which("fli")
    if p:
        return [p]

    # 3. Scripts / bin next to the current Python executable
    for candidate in [
        Path(sys.executable).parent / "Scripts" / "fli.exe",   # Windows
        Path(sys.executable).parent / "Scripts" / "fli",        # Windows no-ext
        Path(sys.executable).parent / "fli",                    # Linux venv
        Path(sys.executable).parent.parent / "bin" / "fli",    # Linux system
    ]:
        if candidate.exists():
            return [str(candidate)]

    raise RuntimeError(
        "fli not found. Run:  pip install flights click"
    )


# ─────────────────────────────────────────────────────────────
# AIRLINE HELPERS
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


# ─────────────────────────────────────────────────────────────
# FLI — BUILD COMMAND
# ─────────────────────────────────────────────────────────────
def _build_cmd(fli: list[str], route: dict,
               departure: str, ret: str) -> list[str]:
    cmd = fli + [
        "flights",
        route["origin"],
        route["destination"],
        departure,
        "--return", ret,
        "--currency", route.get("currency", "EUR"),
        "--sort",    "CHEAPEST",
        "--format",  "json",
    ]

    max_lay = route.get("max_layover_hours")
    if max_lay:
        cmd += ["--max-layover", str(int(float(max_lay) * 60))]

    max_stops = route.get("max_stopovers")
    if max_stops is not None:
        cmd += ["--stops", str(max_stops)]

    dw = route.get("departure_window", {})
    if dw.get("enabled") and dw.get("mode") == "hard":
        fh = dw["from"].split(":")[0]
        th = dw["to"].split(":")[0]
        cmd += ["--time", f"{fh}-{th}"]

    bags = route.get("bags", 0)
    if bags:
        cmd += ["--bags", str(bags)]

    passengers = route.get("passengers", 1)
    if passengers > 1:
        cmd += ["--adults", str(passengers)]

    # Fetch all results when hard airline filter is active,
    # otherwise we might miss the preferred carrier entirely
    al_mode = route.get("preferred_airline_mode", "soft")
    if _get_preferred_airlines(route) and al_mode == "hard":
        cmd += ["--all"]

    return cmd


# ─────────────────────────────────────────────────────────────
# FLI — RUN & PARSE
# ─────────────────────────────────────────────────────────────
def _run_fli(cmd: list[str]) -> list[dict]:
    """Execute fli command and return parsed JSON results."""
    try:
        result = subprocess.run(
            cmd,
            capture_output=True,
            text=True,
            timeout=90,
            encoding="utf-8",
            errors="replace",
        )
    except subprocess.TimeoutExpired:
        log.error("fli timed out after 90s")
        return []
    except Exception as e:
        log.error(f"fli subprocess error: {e}")
        return []

    stdout = (result.stdout or "").strip()
    stderr = (result.stderr or "").strip()

    if not stdout:
        if stderr:
            log.warning(f"fli stderr: {stderr[:300]}")
        return []

    try:
        raw = json.loads(stdout)
    except json.JSONDecodeError:
        log.error(f"fli JSON parse error. Output was: {stdout[:300]}")
        return []

    return _parse_results(raw)


def _parse_results(raw) -> list[dict]:
    """
    Unwrap fli's top-level response envelope and parse each flight.
    fli returns: {"success": true, "count": N, "flights": [...], ...}
    """
    if isinstance(raw, dict):
        # Unwrap the envelope — flights are always under "flights" key
        raw = raw.get("flights", [])
    if not isinstance(raw, list):
        return []

    out = []
    for item in raw:
        try:
            f = _parse_one(item)
            if f:
                out.append(f)
        except Exception as e:
            log.debug(f"Skipping unparseable flight: {e}")
    return out


def _parse_one(item: dict) -> dict | None:
    """
    Parse one flight object from fli's actual JSON structure:
    {
      "price": 812.0, "currency": "EUR",
      "duration": 2720,   # total minutes
      "stops": 2,
      "legs": [
        { "departure_airport": {"code":"OTP","name":"..."},
          "arrival_airport":   {"code":"DOH","name":"..."},
          "departure_time": "2026-09-04T17:10:00",
          "arrival_time":   "2026-09-04T21:40:00",
          "duration": 270,
          "airline": {"code":"QR","name":"Qatar Airways"},
          "flight_number": "222", "aircraft": "Boeing 787" }
      ],
      "layovers": [
        {"airport": {"code":"DOH","name":"..."}, "duration": 1340, "overnight": true}
      ]
    }
    """
    if not isinstance(item, dict):
        return None

    price = item.get("price")
    if price is None:
        return None
    price = float(price)

    duration_min = int(item.get("duration", 0))
    stops        = int(item.get("stops", 0))

    # ── Legs ────────────────────────────────────────────────
    legs = item.get("legs", [])
    if not isinstance(legs, list):
        legs = []

    airlines     = []
    dep_time_str = ""
    dep_hour     = None

    for i, leg in enumerate(legs):
        if not isinstance(leg, dict):
            continue

        # Airline: {"code": "QR", "name": "Qatar Airways"}
        al_obj = leg.get("airline", {})
        al_name = (al_obj.get("name", "") if isinstance(al_obj, dict)
                   else str(al_obj))
        if al_name and al_name not in airlines:
            airlines.append(al_name)

        # Departure time from first leg only
        if i == 0:
            raw_dt = leg.get("departure_time", "")
            if raw_dt:
                try:
                    dt = datetime.fromisoformat(raw_dt)
                    dep_hour     = dt.hour
                    dep_time_str = dt.strftime("%H:%M")
                except Exception:
                    dep_hour     = _extract_hour(raw_dt)
                    dep_time_str = raw_dt[:5] if len(raw_dt) >= 5 else raw_dt

    # ── Layovers (top-level list) ────────────────────────────
    max_layover_min = 0
    for lay in item.get("layovers", []):
        if isinstance(lay, dict):
            max_layover_min = max(max_layover_min, int(lay.get("duration", 0)))

    return {
        "price":           round(price, 2),
        "duration_min":    duration_min,
        "duration_str":    _minutes_to_hm(duration_min),
        "stops":           stops,
        "airline":         ", ".join(airlines) if airlines else "Unknown",
        "airline_codes":   [leg["airline"]["code"].upper()
                            for leg in legs
                            if isinstance(leg, dict)
                            and isinstance(leg.get("airline"), dict)
                            and leg["airline"].get("code")],
        "max_layover_min": max_layover_min,
        "max_layover_h":   round(max_layover_min / 60, 1),
        "dep_hour":        dep_hour,
        "dep_time":        dep_time_str,
        "raw":             item,
    }


def _parse_duration(v) -> int:
    """Convert duration value to minutes (int)."""
    if isinstance(v, (int, float)):
        return int(v)
    if isinstance(v, str):
        v = v.strip()
        mins = 0
        import re
        for m in re.finditer(r"(\d+)\s*h", v):
            mins += int(m.group(1)) * 60
        for m in re.finditer(r"(\d+)\s*m", v):
            mins += int(m.group(1))
        return mins
    return 0


def _minutes_to_hm(m: int) -> str:
    if not m:
        return "?"
    return f"{m // 60}h {m % 60:02d}m"


def _extract_hour(time_str: str) -> int | None:
    """Extract hour (0-23) from a time string like '10:40' or '2027-02-15 22:10'."""
    if not time_str:
        return None
    try:
        part = time_str.strip().split()[-1] if " " in time_str else time_str
        return int(part.split(":")[0])
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────
# FLI — HIGH-LEVEL ROUTE SEARCH
# ─────────────────────────────────────────────────────────────
def _sample_dates(date_from: str, date_to: str, n: int,
                  departure_days: list | None = None) -> list[str]:
    """
    Return up to n evenly-spaced departure dates across [date_from, date_to].
    If departure_days is set (e.g. ['wednesday','thursday','friday']),
    only dates falling on those weekdays are considered.
    """
    start = datetime.strptime(date_from, "%Y-%m-%d")
    end   = datetime.strptime(date_to,   "%Y-%m-%d")

    allowed = {d.lower() for d in departure_days} if departure_days else None

    # Build the pool of eligible dates
    pool = []
    cur  = start
    while cur <= end:
        if allowed is None or cur.strftime("%A").lower() in allowed:
            pool.append(cur.strftime("%Y-%m-%d"))
        cur += timedelta(days=1)

    if not pool:
        log.warning("No dates match the configured departure_days — returning all dates")
        pool = [(start + timedelta(days=i)).strftime("%Y-%m-%d")
                for i in range((end - start).days + 1)]

    if len(pool) <= n:
        return pool

    # Sample n evenly from the pool
    step = (len(pool) - 1) / (n - 1)
    return [pool[round(i * step)] for i in range(n)]


def _return_dates(departure: str, route: dict) -> list[str]:
    """
    Generate return date candidates for a given departure.
    Considers target_nights ± flexibility_days.
    If return_days is configured, only keeps dates on those weekdays.
    Falls back to the exact target date if no candidates match.
    """
    dep         = datetime.strptime(departure, "%Y-%m-%d")
    base        = route.get("target_nights", 20)
    flex        = route.get("flexibility_days", 0)
    return_days = {d.lower() for d in route.get("return_days", [])}

    candidates = []
    for nights in range(max(1, base - flex), base + flex + 1):
        ret = dep + timedelta(days=nights)
        if not return_days or ret.strftime("%A").lower() in return_days:
            candidates.append(ret.strftime("%Y-%m-%d"))

    if not candidates:
        # No date in the window falls on a preferred return day —
        # fall back to exact target so we always search something
        fallback = (dep + timedelta(days=base)).strftime("%Y-%m-%d")
        log.debug(f"  No return day match for dep {departure}, using fallback {fallback}")
        return [fallback]

    return sorted(set(candidates))


def search_route(route: dict, fli_cmd: list[str]) -> list[dict]:
    """
    Sample departure dates across the window and return all flight
    options found, enriched with outbound/return date info.
    """
    n_samples    = route.get("daily_samples", 8)
    dep_dates    = _sample_dates(
        route["date_from"], route["date_to"], n_samples,
        departure_days=route.get("departure_days") or None
    )

    dw = route.get("departure_window", {})
    results = []

    for dep in dep_dates:
        for ret in _return_dates(dep, route):
            nights = (datetime.strptime(ret, "%Y-%m-%d") -
                      datetime.strptime(dep, "%Y-%m-%d")).days
            if nights < 1:
                continue

            cmd = _build_cmd(fli_cmd, route, dep, ret)
            flights = _run_fli(cmd)

            if not flights:
                log.info(f"  {dep} → {ret}: no results")
                continue

            log.info(f"  {dep} → {ret} ({nights}n): "
                     f"{len(flights)} options, best {flights[0]['price']:.0f} {route.get('currency','EUR')}")

            for f in flights:
                f["outbound_date"] = dep
                f["return_date"]   = ret
                f["nights"]        = nights

                # Preferred airline flag (supports multiple airlines)
                # off  = no flagging
                # soft = flag flights containing any preferred carrier with 🏷️
                # hard = flag + filter after collecting all results
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

                results.append(f)

    results.sort(key=lambda x: x["price"])

    preferred_list = _get_preferred_airlines(route)
    al_mode        = route.get("preferred_airline_mode", "soft")
    if preferred_list and al_mode == "hard":
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

    dw   = route.get("departure_window", {})
    lines += [
        "",
        f"💰 Best price: {best['price']:.0f} {currency}{pax_note}",
        f"   🗓  {best['outbound_date']} {best.get('dep_time','')} → {best['return_date']} ({best['nights']} nights)",
        f"   ✈️  {best['airline']}  |  {best['stops']} stop(s)  |  {best['duration_str']}  |  Max layover: {best['max_layover_h']}h",
        f"   {'⭐ Within preferred time window' if best.get('preferred_time') else ('⏰ Outside preferred window' if dw.get('enabled') else '')}",
        "",
        f"📊 Trend: {trend['signal']}",
    ]
    if trend["avg_7d"]:
        lines.append(f"   7d avg: {trend['avg_7d']:.0f}  |  14d avg: {trend['avg_14d']:.0f} {currency}")

    lines += ["", "── Top 5 ──────────────────────────────────────"]
    for i, f in enumerate(top5, 1):
        star     = "⭐ " if f.get("preferred_time") else "   "
        al_star  = "🏷️ " if f.get("preferred_airline_match") and route.get("preferred_airline") else ""
        lines.append(
            f"  {i}. {star}{al_star}{f['price']:.0f} {currency} | "
            f"{f['outbound_date']} {f.get('dep_time','')} → {f['return_date']} ({f['nights']}n) | "
            f"{f['airline']} | {f['stops']} stop(s) | {f['duration_str']}"
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
    gn = cfg["notifications"]
    rn = route.get("notifications", {})
    ch = _active_channels(gn, rn)
    if ch["email"]:    send_email(content,    gn["email"])
    if ch["whatsapp"]: send_whatsapp(content, gn["whatsapp"])
    if ch["ntfy"]:     send_ntfy(content,     gn["ntfy"], route["id"], rn)


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

            top = last.get("top_flights", [])[:5]
            flight_rows = "".join(
                f"<tr style='background:{'#e8f8f0' if f.get('preferred_time') else ''}'>"
                f"<td>{'⭐ ' if f.get('preferred_time') else ''}"
                f"<b>{f['price']:.0f}</b> {currency}</td>"
                f"<td>{f.get('outbound_date','')} <b>{f.get('dep_time','')}</b></td>"
                f"<td>{f.get('return_date','')}</td>"
                f"<td>{f.get('nights','')}n</td>"
                f"<td>{f.get('airline','—')}</td>"
                f"<td>{f.get('stops','?')} / {f.get('duration_str','')}</td>"
                f"<td>{f.get('max_layover_h','')}h</td>"
                f"</tr>"
                for f in top
            )
            flights_tbl = (
                "<table><thead><tr><th>Price</th><th>Depart</th><th>Return</th>"
                "<th>Nights</th><th>Airline</th><th>Stops/Dur</th><th>Lay</th></tr></thead>"
                f"<tbody>{flight_rows}</tbody></table>"
            ) if flight_rows else "<p>No flights today.</p>"

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
          <div style="overflow-x:auto">
            <div style="font-size:11px;color:#888;margin-bottom:4px">
              Today's top options (⭐ = preferred departure time)
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
def process_route(route: dict, cfg: dict, history: dict, fli_cmd: list[str]):
    rid        = route["id"]
    label      = route.get("label", f"{route['origin']}→{route['destination']}")
    currency   = route.get("currency", "EUR")
    passengers = route.get("passengers", 1)

    log.info(f"Searching {label} …")
    flights = search_route(route, fli_cmd)

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
        "top_flights":  flights[:10],
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


def run():
    cfg     = load_config()
    history = load_history()

    try:
        fli_cmd = find_fli()
        log.info(f"fli found: {' '.join(fli_cmd)}")
    except RuntimeError as e:
        log.error(str(e))
        sys.exit(1)

    for route in cfg.get("routes", []):
        log.info(f"\n{'═' * 60}")
        log.info(f"Route: {route.get('label', route['id'])}")
        log.info(f"{'═' * 60}")
        try:
            process_route(route, cfg, history, fli_cmd)
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
    run()
