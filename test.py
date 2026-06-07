#!/usr/bin/env python3
"""
✈️  Flight Tracker — Test Suite
────────────────────────────────────────────────────────────
Run this ONCE after setup to verify everything works before
you push to GitHub and let Actions take over.

    python test.py                  # run all tests
    python test.py --notify         # also send real test notifications
    python test.py --no-live        # skip live flight search (fast)
"""

import argparse
import json
import os
import sys
import tempfile
import time
import traceback
import urllib.parse
from datetime import datetime, timedelta
from pathlib import Path

import requests

# ── colour helpers ────────────────────────────────────────────
GREEN  = "\033[92m"
RED    = "\033[91m"
YELLOW = "\033[93m"
BLUE   = "\033[94m"
RESET  = "\033[0m"
BOLD   = "\033[1m"

def ok(msg):   print(f"  {GREEN}✅ {msg}{RESET}")
def fail(msg): print(f"  {RED}❌ {msg}{RESET}")
def skip(msg): print(f"  {YELLOW}⏭️  {msg}{RESET}")
def info(msg): print(f"  {BLUE}ℹ️  {msg}{RESET}")

results = {"passed": 0, "failed": 0, "skipped": 0}

def test(name: str):
    print(f"\n[TEST] {BOLD}{name}{RESET}")

def record(status: str):
    results[status] += 1


# ─────────────────────────────────────────────────────────────
def run_all(notify: bool, live: bool):
    print(f"\n{BOLD}{'═'*60}{RESET}")
    print(f"{BOLD}  ✈️  Flight Tracker — Test Suite{RESET}")
    print(f"{BOLD}{'═'*60}{RESET}")

    # ── 1. Python version ────────────────────────────────────
    test("Python version")
    v = sys.version_info
    if v >= (3, 10):
        ok(f"Python {v.major}.{v.minor}.{v.micro}")
        record("passed")
    else:
        fail(f"Python {v.major}.{v.minor} — need 3.10+")
        record("failed")

    # ── 2. fli (flights) Python API installed ────────────────
    test("fli (flights) package installed")
    search_flights_cls = None
    try:
        from importlib.metadata import version as pkg_version

        from fli.search import SearchFlights
        search_flights_cls = SearchFlights
        ok(f"flights {pkg_version('flights')} importable (fli.search.SearchFlights)")
        record("passed")
    except ImportError as e:
        fail(f"fli not found: {e} — run: pip install flights")
        record("failed")
    except Exception as e:
        fail(f"Unexpected error: {e}")
        record("failed")

    # ── 4. requests installed ────────────────────────────────
    test("requests installed (notifications dependency)")
    try:
        import requests as req
        ok(f"requests {req.__version__}")
        record("passed")
    except ImportError:
        fail("requests not found — run: pip install requests")
        record("failed")

    # ── 5. Config file ───────────────────────────────────────
    test("config.json")
    cfg = None
    if Path("config.json").exists():
        try:
            with open("config.json", encoding="utf-8") as f:
                cfg = json.load(f)
            routes = cfg.get("routes", [])
            ok(f"Valid JSON — {len(routes)} route(s) configured")
            for r in routes:
                info(f"Route: {r.get('label', r.get('id','?'))}  "
                     f"({r.get('origin','?')}→{r.get('destination','?')})")
            record("passed")
        except json.JSONDecodeError as e:
            fail(f"Invalid JSON: {e}")
            record("failed")
    else:
        skip("config.json not found — copy config.example.json and fill it in")
        record("skipped")

    # ── 6. Live flight search ─────────────────────────────────
    test("Live flight search (fli Python API → Google Flights)")
    if not live:
        skip("Skipped (--no-live flag)")
        record("skipped")
    elif search_flights_cls is None:
        skip("Skipped (fli not found)")
        record("skipped")
    else:
        # Use first configured route, or fall back to a test route
        if cfg and cfg.get("routes"):
            r        = cfg["routes"][0]
            origin   = r["origin"]
            dest     = r["destination"]
            currency = r.get("currency", "EUR")
        else:
            origin, dest, currency = "LHR", "JFK", "EUR"
        dep = (datetime.now() + timedelta(days=90)).strftime("%Y-%m-%d")
        ret = (datetime.now() + timedelta(days=110)).strftime("%Y-%m-%d")

        info(f"Calling: SearchFlights().search({origin} → {dest}, "
             f"{dep} ↔ {ret}, currency={currency})")
        info("(This may take 15–30 seconds…)")

        try:
            from fli.models import (
                Airport,
                FlightSearchFilters,
                FlightSegment,
                PassengerInfo,
                TripType,
            )

            filters = FlightSearchFilters(
                trip_type=TripType.ROUND_TRIP,
                passenger_info=PassengerInfo(adults=1),
                flight_segments=[
                    FlightSegment(
                        departure_airport=[[Airport[origin.upper()], 0]],
                        arrival_airport=[[Airport[dest.upper()], 0]],
                        travel_date=dep,
                    ),
                    FlightSegment(
                        departure_airport=[[Airport[dest.upper()], 0]],
                        arrival_airport=[[Airport[origin.upper()], 0]],
                        travel_date=ret,
                    ),
                ],
            )
            search_results = search_flights_cls().search(filters, top_n=5, currency=currency)

            if search_results:
                ok(f"Got {len(search_results)} round-trip option(s)")
                outbound, _return_flight = search_results[0]
                info(f"Best price: {outbound.price} {currency}")
                record("passed")
            else:
                fail("No results returned — possible rate limit or no availability")
                record("failed")
        except KeyError as e:
            fail(f"Unknown airport code: {e} — check route['origin']/['destination']")
            record("failed")
        except Exception as e:
            if "rate" in str(e).lower() or "429" in str(e):
                fail("Rate limited by Google — wait 30 min and try again")
            else:
                fail(f"fli search error: {e}")
            record("failed")

    # ── 7. Price history read/write ───────────────────────────
    test("Price history (read/write price_history.json)")
    try:
        from flight_tracker import load_history, save_history, add_entry
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False,
                                         mode="w") as tmp:
            tmp_path = tmp.name

        import flight_tracker as ft
        orig = ft.HISTORY_FILE
        ft.HISTORY_FILE = tmp_path

        h = ft.load_history()
        ft.add_entry(h, "test-route", "Test Route", {
            "date": "2026-01-01", "best_price": 999.0,
            "currency": "EUR", "outbound_date": "2027-02-01",
            "return_date": "2027-02-21", "nights": 20,
            "airline": "Test Air", "stops": 1,
            "duration_str": "24h 00m", "top_flights": [],
        })
        ft.save_history(h)
        h2 = ft.load_history()

        assert "test-route" in h2, "Route not saved"
        assert h2["test-route"]["entries"][0]["best_price"] == 999.0

        ft.HISTORY_FILE = orig
        Path(tmp_path).unlink(missing_ok=True)
        ok("Read/write working correctly")
        record("passed")
    except Exception as e:
        fail(f"History error: {e}")
        record("failed")

    # ── 8. Dashboard generation ───────────────────────────────
    test("Dashboard generation (docs/index.html)")
    try:
        from flight_tracker import generate_dashboard
        dummy_routes = [{
            "id": "test", "label": "Test → Route",
            "origin": "TST", "destination": "RTE",
            "currency": "EUR", "preferred_airline": "",
            "departure_window": {"enabled": False},
        }]
        dummy_history = {
            "test": {
                "label": "Test → Route",
                "entries": [
                    {"date": "2026-06-01", "best_price": 1200.0},
                    {"date": "2026-06-02", "best_price": 1150.0},
                    {"date": "2026-06-03", "best_price": 1100.0},
                ],
            }
        }
        html = generate_dashboard(dummy_routes, dummy_history)
        assert "<html" in html and "chart.js" in html
        ok(f"Generated {len(html):,} bytes of valid HTML")
        record("passed")
    except Exception as e:
        fail(f"Dashboard error: {e}")
        record("failed")

    # ── 9. Email notification ─────────────────────────────────
    test("Email notification")
    if not notify or not cfg:
        skip("Skipped (pass --notify to test, or no config)")
        record("skipped")
    else:
        ec = cfg.get("notifications", {}).get("email", {})
        if not ec.get("enabled"):
            skip("Email disabled in config")
            record("skipped")
        else:
            from flight_tracker import send_email
            import smtplib
            try:
                send_email(
                    {"subject": "✈️ Flight Tracker — Test Email",
                     "plain": "This is a test message from Flight Tracker.\nEverything is working!",
                     "html": "<h2>✈️ Flight Tracker</h2><p>Test email — everything is working!</p>"},
                    ec
                )
                # Check the log for errors by re-attempting with direct SMTP
                import smtplib
                with smtplib.SMTP(ec["smtp_server"], ec["smtp_port"]) as s:
                    s.ehlo(); s.starttls()
                    s.login(ec["username"], ec["password"])
                ok("Test email sent — check your inbox")
                record("passed")
            except smtplib.SMTPAuthenticationError:
                fail("Gmail auth failed — make sure you're using an App Password, not your regular password")
                info("Get one at: https://myaccount.google.com/apppasswords")
                record("failed")
            except Exception as e:
                fail(f"Email failed: {e}")
                record("failed")

    # ── 10. WhatsApp notification ─────────────────────────────
    test("WhatsApp notification (CallMeBot)")
    if not notify or not cfg:
        skip("Skipped (pass --notify to test, or no config)")
        record("skipped")
    else:
        wc = cfg.get("notifications", {}).get("whatsapp", {})
        if not wc.get("enabled"):
            skip("WhatsApp disabled in config")
            record("skipped")
        else:
            from flight_tracker import send_whatsapp
            try:
                send_whatsapp(
                    {"plain": "✈️ Flight Tracker test message — everything is working!"},
                    wc
                )
                ok("Test WhatsApp message sent — check your phone")
                record("passed")
            except Exception as e:
                fail(f"WhatsApp failed: {e}")
                record("failed")

    # ── 11. ntfy notification ─────────────────────────────────
    test("ntfy.sh notification")
    if not notify or not cfg:
        skip("Skipped (pass --notify to test, or no config)")
        record("skipped")
    else:
        nc = cfg.get("notifications", {}).get("ntfy", {})
        if not nc.get("enabled"):
            skip("ntfy disabled in config")
            record("skipped")
        else:
            from flight_tracker import send_ntfy
            try:
                send_ntfy(
                    {"subject": "✈️ Flight Tracker — Test",
                     "plain": "✈️ Flight Tracker test message — everything is working!"},
                    nc,
                    "test",
                    {}
                )
                ok("Test ntfy message sent — check your phone")
                record("passed")
            except Exception as e:
                fail(f"ntfy failed: {e}")
                record("failed")

    # ── 12. GitHub Actions config ─────────────────────────────
    test("GitHub Actions workflow file")
    wf = Path(".github/workflows/flight_check.yml")
    if wf.exists():
        ok(f"Workflow file found: {wf}")
        record("passed")
    else:
        skip(f"Not found — needed for automatic daily runs")
        record("skipped")

    # ── Summary ───────────────────────────────────────────────
    print(f"\n{BOLD}{'═'*60}{RESET}")
    total = results["passed"] + results["failed"] + results["skipped"]
    p, f_, s = results["passed"], results["failed"], results["skipped"]
    colour = GREEN if f_ == 0 else RED
    print(f"{colour}{BOLD}  Results: {p} passed · {f_} failed · {s} skipped  "
          f"(of {total} tests){RESET}")
    print(f"{BOLD}{'═'*60}{RESET}\n")

    if f_ > 0:
        print(f"{RED}Fix the failing tests above before pushing to GitHub.{RESET}\n")
    elif s > 0 and not notify:
        print(f"{YELLOW}Tip: run  python test.py --notify  to also test notification channels.{RESET}\n")
    else:
        print(f"{GREEN}All good — you're ready to push to GitHub! 🚀{RESET}\n")

    return f_ == 0


# ─────────────────────────────────────────────────────────────
if __name__ == "__main__":
    parser = argparse.ArgumentParser(
        description="Flight Tracker test suite"
    )
    parser.add_argument(
        "--notify", action="store_true",
        help="Send real test notifications to all enabled channels"
    )
    parser.add_argument(
        "--no-live", action="store_true",
        help="Skip the live flight search test (faster, no rate limiting risk)"
    )
    args = parser.parse_args()

    success = run_all(notify=args.notify, live=not args.no_live)
    sys.exit(0 if success else 1)
