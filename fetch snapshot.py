#!/usr/bin/env python3
"""
fetch_snapshot.py - FlightScope daily snapshot fetcher

Fetches cheapest cached fares from Aviasales' public price-map endpoint
and writes them to flight-data.json which the HTML reads at runtime.

Usage:
    python3 fetch_snapshot.py
    python3 fetch_snapshot.py --origins BKK,SIN,FRA,LHR
    python3 fetch_snapshot.py --origin BKK --period season

No API key. Aviasales' price-map endpoint is the same one that powers
their public map.aviasales.com site. Their cache lags real prices by
up to 7 days, so weekly refresh is the right cadence.
"""
from __future__ import annotations

import argparse
import json
import sys
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

AVIASALES_URL = "https://map.aviasales.com/prices.json"
DEFAULT_ORIGINS = ["BKK", "SIN", "FRA", "LHR", "DXB"]


def fetch_origin(origin: str, period: str = "year") -> list[dict[str, Any]]:
    """Fetch all price-map entries for one origin IATA code."""
    params = {
        "origin_iata": origin,
        "period": period,
        "one_way": "false",
        "locale": "en",
    }
    url = f"{AVIASALES_URL}?{urllib.parse.urlencode(params)}"
    req = urllib.request.Request(
        url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; FlightScope-snapshot)",
            "Accept": "application/json",
        },
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = json.loads(resp.read())
    if not isinstance(data, list):
        raise ValueError(f"Unexpected response shape for {origin}")
    return data


def normalise(items: list[dict[str, Any]], origin: str) -> list[dict[str, Any]]:
    """Filter to records with prices, tag with origin, drop noise fields."""
    out = []
    for item in items:
        if not item.get("value") or not item.get("destination"):
            continue
        out.append({
            "origin": origin,
            "destination": item.get("destination"),
            "value": item.get("value"),
            "depart_date": item.get("depart_date"),
            "return_date": item.get("return_date"),
            "number_of_changes": item.get("number_of_changes", 0),
            "gate": item.get("gate") or "Aviasales",
            "distance": item.get("distance"),
        })
    return out


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch FlightScope daily snapshot")
    parser.add_argument("--origin", default=None, help="Single origin IATA")
    parser.add_argument(
        "--origins", default=None,
        help=f"Comma-separated origins (default: {','.join(DEFAULT_ORIGINS)})",
    )
    parser.add_argument(
        "--period", default="year", choices=["year", "season", "month"],
    )
    parser.add_argument("--out", default="flight-data.json")
    args = parser.parse_args()

    if args.origins:
        origins = [o.strip().upper() for o in args.origins.split(",") if o.strip()]
    elif args.origin:
        origins = [args.origin.strip().upper()]
    else:
        origins = DEFAULT_ORIGINS

    all_routes: list[dict[str, Any]] = []
    successes: list[tuple[str, int]] = []
    failures: list[tuple[str, str]] = []

    for origin in origins:
        print(f"  fetching {origin} period={args.period}...", end=" ", flush=True)
        try:
            raw = fetch_origin(origin, period=args.period)
            routes = normalise(raw, origin)
            all_routes.extend(routes)
            successes.append((origin, len(routes)))
            print(f"got {len(routes)} routes")
        except Exception as exc:
            failures.append((origin, str(exc)))
            print(f"FAIL ({exc})")

    if not all_routes:
        print("\nNo routes fetched. Aborting.", file=sys.stderr)
        return 1

    snapshot = {
        "meta": {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "origins": [o for o, _ in successes],
            "period": args.period,
            "currency_raw": "RUB",
            "source": "Aviasales price-map (cached, up to 7 days delayed)",
            "route_count": len(all_routes),
            "build_note": "Tuesday morning is when airlines push fare sales for the upcoming week, so Tuesday refresh catches the freshest weekly cycle.",
        },
        "routes": all_routes,
    }

    out_path = Path(args.out)
    out_path.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")

    print(f"\nWrote {len(all_routes)} routes to {out_path}")
    print(f"  Origins succeeded: {[o for o, _ in successes]}")
    if failures:
        print(f"  Origins failed:    {[o for o, _ in failures]}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
