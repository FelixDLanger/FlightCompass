#!/usr/bin/env python3
"""
fetch_hotels.py - FlightCompass weekly hotel snapshot fetcher

Uses the Hotellook /cache.json endpoint (Travelpayouts ecosystem).
Free to access; no token required for cache endpoint.
Docs: https://support.travelpayouts.com/hc/en-us/articles/115001372487

Returns median hotel rates for a fixed list of cities. The output JSON is
read by index.html to populate the trip-cost optimizer with live data
instead of the hand-curated HOTEL_RATES_EUR fallback.

Usage:
    python3 fetch_hotels.py
    python3 fetch_hotels.py --output hotel-data.json
    python3 fetch_hotels.py --check-in 2026-08-15 --nights 3

The cache endpoint accepts location (city IATA OR full city name),
currency, checkIn, checkOut, and limit. Returns up to N hotels with
price snapshots. We aggregate to a median to surface a "typical" rate.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

API_BASE = "https://engine.hotellook.com/api/v2/cache.json"

# Cities aligned with FlightCompass destination set.
# Using IATA codes (Hotellook accepts city IATA for major cities).
# This is the same set used in fetch_snapshot.py for symmetry.
DEFAULT_CITIES = [
    # Asia
    "BKK","SIN","KUL","CGK","MNL","HKG","TPE","ICN","NRT","HND","KIX","BOM","DEL",
    "BLR","SGN","HAN","DPS","PNH","REP","RGN","CMB","KTM","PEK","PVG","CAN","SZX",
    "MFM","HKT","DAD","CXR",
    # Europe
    "LHR","LGW","CDG","FRA","MUC","BER","AMS","MAD","BCN","PMI","FCO","MXP",
    "NAP","LIS","OPO","ATH","JTR","ZRH","VIE","ARN","CPH","OSL","HEL","DUB","PRG",
    "BUD","WAW","IST",
    # Middle East
    "DXB","DOH","AUH","RUH","JED","AMM","TLV","MCT","KWI","BAH",
    # Africa
    "JNB","CPT","NBO","CAI","CMN","ADD",
    # Americas
    "LAX","JFK","SFO","ORD","MIA","DFW","SEA","YYZ","YVR","MEX","CUN","GRU","GIG",
    "EZE","SCL","LIM","BOG",
    # Oceania
    "MEL","SYD","BNE","PER","AKL","CHC",
]


def fetch_city(city: str, check_in: str, check_out: str, limit: int = 40) -> list[dict[str, Any]]:
    """Fetch cached hotel prices for one city.

    Returns list of hotel snapshots: each includes hotelName, priceFrom (EUR), stars, etc.
    """
    params = {
        "location": city,
        "currency": "eur",
        "checkIn": check_in,
        "checkOut": check_out,
        "limit": limit,
    }
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "FlightCompass/1.0 (hotel-snapshot-fetcher)",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            time.sleep(2)
            return fetch_city(city, check_in, check_out, limit)
        print(f"  WARN {city}: HTTP {exc.code}", file=sys.stderr)
        return []
    except (urllib.error.URLError, json.JSONDecodeError) as exc:
        print(f"  WARN {city}: {exc}", file=sys.stderr)
        return []
    # Hotellook cache returns either a list directly or an object with results
    if isinstance(payload, list):
        return payload
    if isinstance(payload, dict):
        return payload.get("results") or payload.get("data") or []
    return []


def aggregate_city(items: list[dict[str, Any]]) -> dict[str, Any] | None:
    """Compute median and percentile-based summary for a city's hotel snapshot.

    We exclude obvious outliers (top 10% to filter ultra-luxury that skews the median
    for a "typical 4-star" benchmark). Returns None if too few data points.
    """
    if not items:
        return None
    prices = []
    star_buckets: dict[int, list[float]] = {}
    for item in items:
        price = item.get("priceFrom") or item.get("price_from") or item.get("priceAvg")
        if not price or price <= 0:
            continue
        try:
            price = float(price)
        except (TypeError, ValueError):
            continue
        prices.append(price)
        stars = item.get("stars")
        try:
            star_int = int(round(float(stars))) if stars else 0
        except (TypeError, ValueError):
            star_int = 0
        if star_int in (3, 4, 5):
            star_buckets.setdefault(star_int, []).append(price)
    if len(prices) < 3:
        return None
    prices.sort()
    # Trim top 10% as outlier control (luxury skew)
    trim = max(1, len(prices) // 10)
    trimmed = prices[:-trim] if len(prices) > trim else prices
    summary = {
        "median_eur": round(statistics.median(trimmed), 2),
        "p25_eur": round(statistics.quantiles(trimmed, n=4)[0], 2) if len(trimmed) >= 4 else round(min(trimmed), 2),
        "p75_eur": round(statistics.quantiles(trimmed, n=4)[2], 2) if len(trimmed) >= 4 else round(max(trimmed), 2),
        "sample_size": len(prices),
    }
    # Star-bucket medians (preferred display: 4-star as the "typical traveller" reference)
    for star, plist in star_buckets.items():
        if len(plist) >= 2:
            summary[f"median_{star}star_eur"] = round(statistics.median(plist), 2)
    return summary


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch hotel price snapshot for FlightCompass")
    parser.add_argument("--output", default="hotel-data.json", help="Output JSON path")
    parser.add_argument(
        "--check-in",
        default=None,
        help="Check-in date YYYY-MM-DD (default: today + 30 days)",
    )
    parser.add_argument(
        "--nights",
        type=int,
        default=3,
        help="Number of nights (default: 3 — typical short-trip benchmark)",
    )
    parser.add_argument(
        "--cities",
        default=None,
        help="Comma-separated city IATA codes to fetch (default: full list)",
    )
    args = parser.parse_args()

    # Default check-in: 30 days from today (Hotellook cache works best for forward dates)
    if args.check_in:
        check_in = args.check_in
    else:
        ci = date.today() + timedelta(days=30)
        check_in = ci.isoformat()
    co = datetime.fromisoformat(check_in).date() + timedelta(days=args.nights)
    check_out = co.isoformat()

    cities = [c.strip().upper() for c in args.cities.split(",")] if args.cities else DEFAULT_CITIES

    print(f"Fetching hotel snapshot for {len(cities)} cities")
    print(f"Check-in: {check_in}  Check-out: {check_out}  ({args.nights} nights)")
    print()

    out: dict[str, Any] = {
        "meta": {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "Hotellook cache.json",
            "check_in": check_in,
            "check_out": check_out,
            "nights": args.nights,
            "currency": "EUR",
            "city_count": 0,
            "note": "Median prices exclude top 10% (luxury skew). Sample sizes vary by city.",
        },
        "cities": {},
    }

    total_ok = 0
    for city in cities:
        items = fetch_city(city, check_in, check_out)
        summary = aggregate_city(items)
        if summary:
            out["cities"][city] = summary
            total_ok += 1
            print(f"  {city}: median €{summary['median_eur']} (n={summary['sample_size']})")
        else:
            print(f"  {city}: no data")
        time.sleep(0.1)  # polite to the API

    out["meta"]["city_count"] = total_ok
    Path(args.output).write_text(json.dumps(out, indent=2, ensure_ascii=False))
    print()
    print(f"Wrote {args.output} ({total_ok}/{len(cities)} cities with usable data)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
