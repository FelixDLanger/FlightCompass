#!/usr/bin/env python3
"""
fetch_snapshot.py - FlightCompass weekly snapshot fetcher

Uses the Travelpayouts Data API (the proper, token-authenticated version
of the Aviasales price cache). Free to access after signing up at
https://www.travelpayouts.com/.

Get your token: profile -> API token section.
Set as GitHub Actions secret named TRAVELPAYOUTS_TOKEN.

Usage:
    TRAVELPAYOUTS_TOKEN=xxx python3 fetch_snapshot.py
    TRAVELPAYOUTS_TOKEN=xxx python3 fetch_snapshot.py --origins BKK,SIN,FRA,LHR,DXB
    TRAVELPAYOUTS_TOKEN=xxx python3 fetch_snapshot.py --month 2026-11
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

API_BASE = "https://api.travelpayouts.com/aviasales/v3/prices_for_dates"
DEFAULT_ORIGINS = [
    # SEA hubs (primary user base)
    "BKK", "SIN", "KUL", "CGK", "MNL", "DPS", "SGN", "HAN",
    # East Asia (deep APAC coverage)
    "HKG", "TPE", "ICN", "NRT", "HND", "KIX", "PEK", "PVG", "CAN", "SZX",
    # South Asia
    "BOM", "DEL", "BLR",
    # Middle East hubs
    "DXB", "DOH", "AUH", "RUH",
    # Europe major (second base)
    "LHR", "LGW", "CDG", "ORY", "FRA", "MUC", "BER", "AMS", "MAD", "BCN",
    "FCO", "MXP", "ZRH", "VIE", "IST", "ATH",
    # North America
    "LAX", "JFK", "SFO", "ORD", "MIA", "DFW", "YYZ",
    # Oceania
    "SYD", "MEL", "AKL",
    # Africa
    "JNB", "CPT", "CAI", "NBO",
    # South & Central America
    "GRU", "GIG", "EZE", "MEX", "BOG",
]

# Major destination IATA codes to query per origin. Travelpayouts requires
# either origin+destination or one of the date filters.
DEFAULT_DESTINATIONS = [
    # Asia (deeper coverage)
    "BKK","SIN","KUL","CGK","MNL","HKG","TPE","ICN","NRT","HND","KIX","BOM","DEL",
    "BLR","SGN","HAN","DPS","PNH","REP","RGN","CMB","KTM","PEK","PVG","CAN","SZX",
    "MFM","HKT","DAD","CXR",
    # Europe (expanded secondary cities)
    "LHR","LGW","CDG","ORY","FRA","MUC","BER","AMS","MAD","BCN","PMI","FCO","MXP",
    "NAP","LIS","OPO","ATH","JTR","ZRH","VIE","ARN","CPH","OSL","HEL","DUB","PRG",
    "BUD","WAW","IST","SAW",
    # Middle East
    "DXB","DOH","AUH","RUH","JED","AMM","TLV","MCT","KWI","BAH",
    # Africa
    "JNB","CPT","NBO","CAI","CMN","ADD","DAR","MRU",
    # Americas
    "LAX","JFK","SFO","ORD","MIA","DFW","SEA","YYZ","YVR","MEX","CUN","GRU","GIG",
    "EZE","SCL","LIM","BOG",
    # Oceania
    "MEL","SYD","BNE","PER","AKL","CHC","NAN",
]


def fetch_route(origin: str, destination: str, month: str | None, token: str) -> list[dict[str, Any]]:
    """Fetch cheapest tickets for one origin->destination, optionally for a specific month."""
    if origin == destination:
        return []
    params = {
        "origin": origin,
        "destination": destination,
        "currency": "eur",
        "sorting": "price",
        "direct": "false",
        "limit": 30,
        "one_way": "false",
        "token": token,
    }
    if month:
        params["departure_at"] = month  # YYYY-MM or YYYY-MM-DD
    url = f"{API_BASE}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "FlightCompass/1.0 (snapshot-fetcher)",
            "Accept": "application/json",
            "X-Access-Token": token,
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            payload = json.loads(resp.read())
    except urllib.error.HTTPError as exc:
        if exc.code == 429:
            time.sleep(2)
            return fetch_route(origin, destination, month, token)
        raise
    if not payload.get("success"):
        return []
    data = payload.get("data") or []
    if not isinstance(data, list):
        return []
    return data


def normalise(items: list[dict[str, Any]], origin: str) -> list[dict[str, Any]]:
    """Normalise Travelpayouts response items into our snapshot schema."""
    out = []
    for item in items:
        if not item.get("price") or not item.get("destination"):
            continue
        out.append({
            "origin": item.get("origin") or origin,
            "destination": item.get("destination"),
            # NOTE: with currency=eur, "price" is in EUR (not RUB).
            # HTML will treat .value as EUR if .currency=="EUR"
            "value": item.get("price"),
            "currency": "EUR",
            # Keep date for filtering compatibility
            "depart_date": item.get("departure_at", "").split("T")[0] if item.get("departure_at") else None,
            "return_date": item.get("return_at", "").split("T")[0] if item.get("return_at") else None,
            # Full ISO datetimes (UTC) for richer rendering
            "depart_at": item.get("departure_at"),
            "return_at": item.get("return_at"),
            "number_of_changes": item.get("transfers", 0),
            "gate": item.get("airline") or "Aviasales",
            "flight_number": item.get("flight_number"),
            "duration_to": item.get("duration_to"),    # outbound flight minutes
            "duration_back": item.get("duration_back"),  # return flight minutes
            "duration": item.get("duration"),  # total trip
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch FlightCompass snapshot via Travelpayouts API")
    parser.add_argument("--origin", default=None, help="Single origin IATA")
    parser.add_argument("--origins", default=None, help=f"Comma-separated (default: {','.join(DEFAULT_ORIGINS)})")
    parser.add_argument("--destinations", default=None, help="Comma-separated destinations (default: ~60 major hubs)")
    parser.add_argument("--month", default=None, help="Departure month filter: YYYY-MM (e.g. 2026-11). Default: no filter.")
    parser.add_argument("--out", default="flight-data.json")
    parser.add_argument("--token", default=None, help="Travelpayouts API token (or set TRAVELPAYOUTS_TOKEN env var)")
    args = parser.parse_args()

    token = args.token or os.environ.get("TRAVELPAYOUTS_TOKEN")
    if not token:
        print("ERROR: no token. Pass --token or set TRAVELPAYOUTS_TOKEN env var.", file=sys.stderr)
        print("Get a token at https://www.travelpayouts.com (free signup).", file=sys.stderr)
        return 2

    if args.origins:
        origins = [o.strip().upper() for o in args.origins.split(",") if o.strip()]
    elif args.origin:
        origins = [args.origin.strip().upper()]
    else:
        origins = DEFAULT_ORIGINS

    destinations = (
        [d.strip().upper() for d in args.destinations.split(",") if d.strip()]
        if args.destinations
        else DEFAULT_DESTINATIONS
    )

    all_routes: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    for origin in origins:
        origin_count = 0
        for destination in destinations:
            if destination == origin:
                continue
            try:
                raw = fetch_route(origin, destination, args.month, token)
                routes = normalise(raw, origin)
                all_routes.extend(routes)
                origin_count += len(routes)
                # Small delay to be polite to the API
                time.sleep(0.05)
            except Exception as exc:
                print(f"  WARN {origin}->{destination}: {exc}", file=sys.stderr)
        counts[origin] = origin_count
        print(f"  {origin}: {origin_count} routes")

    if not all_routes:
        print("\nNo routes fetched. Aborting (snapshot not overwritten).", file=sys.stderr)
        return 1

    snapshot = {
        "meta": {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "origins": list(counts.keys()),
            "month_filter": args.month,
            "currency": "EUR",
            "source": "Travelpayouts Data API (Aviasales cache, 2-7 day TTL)",
            "route_count": len(all_routes),
            "counts_by_origin": counts,
            "build_note": "Tuesday refresh catches airlines' weekly sale cycle.",
        },
        "routes": all_routes,
    }

    out_path = Path(args.out)
    out_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nWrote {len(all_routes)} routes to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
