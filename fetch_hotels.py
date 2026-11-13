#!/usr/bin/env python3
"""
fetch_hotels.py - FlightCompass weekly hotel-rate fetcher (Agoda Affiliate Lite API)

For each destination city it asks Agoda for 4-star-and-up hotels on one sample stay
(a Wednesday about four weeks out, 2 nights, 2 adults) and stores the median nightly
rate of the 4-star ones in hotel-data.json. The page uses that figure in its trip cost
check and falls back to its built-in benchmark for any city without a fresh rate.

Credentials come from the environment and are never written anywhere:
    AGODA_SITE_ID   your Agoda site ID (public, it is the cid in every Agoda link)
    AGODA_API_KEY   your Agoda API key (secret: GitHub Actions secret only)

Usage:
    python3 fetch_hotels.py            # all cities -> hotel-data.json
    python3 fetch_hotels.py --probe    # Bangkok only, prints the response shape, writes nothing
"""
from __future__ import annotations

import argparse
import gzip
import json
import os
import re
import statistics
import sys
import time
import urllib.error
import urllib.request
import zlib
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Any

API_HOST_PATH = "affiliateapi7643.agoda.com/affiliateservice/lt_v1"
MIN_RESULTS = 5        # fewer 4-star results than this = no figure for the city
MIN_CITIES = 20        # fewer cities than this = treat the run as failed, keep the old file
STAY_NIGHTS = 2

# Page city name -> Agoda city_id (from Agoda's hotel data file of 12 Nov 2026)
CITY_IDS: dict[str, int] = {
    "Abu Dhabi": 10182, "Addis Ababa": 21035, "Amman": 15806, "Amsterdam": 13868, "Athens": 16571,
    "Auckland": 3750, "Bali": 17193, "Bangalore": 4923, "Bangkok": 9395, "Barcelona": 2002, "Beijing": 1569,
    "Berlin": 2366, "Bogota": 4926, "Brisbane": 9466, "Budapest": 10647, "Buenos Aires": 9294, "Cairo": 7923,
    "Cancun": 5954, "Cape Town": 1063, "Casablanca": 15368, "Chicago": 13899, "Christchurch": 6167,
    "Colombo": 7835, "Copenhagen": 16480, "Da Nang": 16440, "Dallas": 8683, "Dar es Salaam": 18094,
    "Delhi": 14552, "Denpasar (Bali)": 17193, "Doha": 4472, "Dubai": 2994, "Dublin": 1131,
    "Frankfurt": 15847, "Guangzhou": 10112, "Hanoi": 2758, "Helsinki": 11154, "Ho Chi Minh": 13170,
    "Hong Kong": 16808, "Istanbul": 14932, "Jakarta": 8691, "Jeddah": 9777, "Johannesburg": 5663,
    "Kathmandu": 2487, "Kuala Lumpur": 14524, "Kuwait City": 16918, "Lima": 7082, "Lisbon": 16364,
    "London": 233, "Los Angeles": 12772, "Macau": 21397, "Madrid": 5531, "Manama": 16630, "Manila": 1622,
    "Melbourne": 10372, "Mexico City": 2086, "Miami": 15289, "Milan": 16585, "Mumbai": 16850,
    "Munich": 16017, "Muscat": 6445, "Nadi": 5057, "Nairobi": 8944, "Naples": 16572, "New York": 318,
    "Nha Trang": 2679, "Osaka": 9590, "Oslo": 12746, "Palma": 16716, "Paris": 15470, "Perth": 11980,
    "Phnom Penh": 4816, "Phuket": 16056, "Port Louis": 19226, "Porto": 8738, "Prague": 15977,
    "Rio de Janeiro": 7395, "Riyadh": 5349, "Rome": 16594, "San Francisco": 13801, "San Jose": 6576,
    "Santiago": 86197, "Santorini": 16084, "Sao Paulo": 16638, "Seattle": 3687, "Seoul": 14690,
    "Shanghai": 3987, "Shenzhen": 12333, "Siem Reap": 16917, "Singapore": 4064, "Stockholm": 1618,
    "Sydney": 14370, "Taipei": 4951, "Tel Aviv": 1692, "Tokyo": 5085, "Toronto": 4495, "Vancouver": 9023,
    "Vienna": 16582, "Vientiane": 10871, "Warsaw": 16587, "Yangon": 16599, "Zurich": 16462,
}


class AuthError(Exception):
    """Agoda rejected the credentials or the quota is used up; retrying will not help."""


def sample_stay(today: date | None = None) -> tuple[str, str]:
    """Check-in on the first Wednesday at least 28 days out, STAY_NIGHTS nights."""
    d = (today or date.today()) + timedelta(days=28)
    d += timedelta(days=(2 - d.weekday()) % 7)  # Monday=0, Wednesday=2
    return d.isoformat(), (d + timedelta(days=STAY_NIGHTS)).isoformat()


def _decode(raw: bytes, encoding: str) -> str:
    enc = (encoding or "").lower()
    if "gzip" in enc:
        raw = gzip.decompress(raw)
    elif "deflate" in enc:
        try:
            raw = zlib.decompress(raw)
        except zlib.error:
            raw = zlib.decompress(raw, -zlib.MAX_WBITS)
    return raw.decode("utf-8", "replace")


def call_api(base_url: str, site_id: str, api_key: str, city_id: int, check_in: str, check_out: str) -> tuple[int, Any]:
    """One city search. Returns (http_status, parsed_json_or_None)."""
    body = {
        "criteria": {
            "additional": {
                "currency": "EUR",
                "language": "en-us",
                "maxResult": 30,
                "minimumStarRating": 4,
                "sortBy": "Recommended",
                "occupancy": {"numberOfAdult": 2, "numberOfChildren": 0},
            },
            "checkInDate": check_in,
            "checkOutDate": check_out,
            "cityId": city_id,
        }
    }
    req = urllib.request.Request(
        base_url,
        data=json.dumps(body).encode("utf-8"),
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Accept": "application/json",
            "Accept-Encoding": "gzip,deflate",
            "Authorization": f"{site_id}:{api_key}",
            "User-Agent": "FlightCompass/1.0 (hotel-fetcher)",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            status, text = resp.status, _decode(resp.read(), resp.headers.get("Content-Encoding", ""))
    except urllib.error.HTTPError as exc:
        status, text = exc.code, _decode(exc.read() or b"", exc.headers.get("Content-Encoding", "") if exc.headers else "")
    if status in (401, 403):
        raise AuthError(f"HTTP {status}: {text[:200]}")
    try:
        return status, json.loads(text) if text.strip() else None
    except ValueError:
        return status, None


def search_city(base_urls: list[str], site_id: str, api_key: str, city_id: int, check_in: str, check_out: str) -> tuple[int, Any]:
    """Tries HTTPS first, then the documented HTTP endpoint; retries server errors and timeouts twice."""
    last: tuple[int, Any] = (0, None)
    for base in list(base_urls):
        for attempt in range(3):
            try:
                status, payload = call_api(base, site_id, api_key, city_id, check_in, check_out)
            except AuthError:
                raise
            except Exception as exc:  # network error or timeout
                last = (0, {"error": {"message": str(exc)[:160]}})
                time.sleep(2 * (attempt + 1))
                continue
            last = (status, payload)
            if status >= 500:
                time.sleep(2 * (attempt + 1))
                continue
            if base_urls[0] != base:  # this endpoint works: use it first from now on
                base_urls.remove(base)
                base_urls.insert(0, base)
            return last
    return last


def four_star_rates(payload: Any) -> list[float]:
    """Nightly EUR rates of the 4-star hotels (4.0 to 4.5) in a response."""
    if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
        return []
    rates = []
    for h in payload["results"]:
        if not isinstance(h, dict) or str(h.get("currency", "EUR")).upper() != "EUR":
            continue
        try:
            star, rate = float(h.get("starRating") or 0), float(h.get("dailyRate") or 0)
        except (TypeError, ValueError):
            continue
        if 4 <= star < 5 and 5 < rate < 5000:
            rates.append(rate)
    return rates


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch Agoda 4-star nightly rates for FlightCompass")
    parser.add_argument("--probe", action="store_true", help="Bangkok only: print the response shape, write nothing")
    parser.add_argument("--out", default="hotel-data.json")
    parser.add_argument("--base-url", default=None, help="Override the API endpoint (testing)")
    args = parser.parse_args()

    site_id = (os.environ.get("AGODA_SITE_ID") or "").strip()
    api_key = (os.environ.get("AGODA_API_KEY") or "").strip().strip('"').strip("'").strip()
    # Accept a secret pasted in the header's own "siteid:apikey" form; the site ID in it belongs to the key
    m = re.fullmatch(r"(\d{3,12})\s*:\s*(\S+)", api_key)
    if m:
        if m.group(1) != site_id:
            print(f"Note: the API key secret includes site ID {m.group(1)}; using that site ID.")
        site_id, api_key = m.group(1), m.group(2)
    if not site_id.isdigit() or not api_key:
        print("ERROR: set AGODA_SITE_ID (numeric) and AGODA_API_KEY.", file=sys.stderr)
        return 2

    base_urls = [args.base_url] if args.base_url else [f"https://{API_HOST_PATH}", f"http://{API_HOST_PATH}"]
    check_in, check_out = sample_stay()

    if args.probe:
        try:
            status, payload = search_city(base_urls, site_id, api_key, CITY_IDS["Bangkok"], check_in, check_out)
        except AuthError as exc:
            print(f"PROBE FAILED: Agoda rejected the request ({exc}).")
            # Shape of what was sent, never its content
            guid = bool(re.fullmatch(r"[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}", api_key))
            print(f"  site ID sent: {site_id}")
            print(f"  API key sent: {len(api_key)} characters, GUID format (8-4-4-4-12): {'yes' if guid else 'no'}, "
                  f"contains spaces: {'yes' if re.search(r'[ \t]', api_key) else 'no'}")
            print("  Compare both with the API page of the Agoda partner portal: the site ID shown next to the key must match.")
            return 1
        results = payload.get("results") if isinstance(payload, dict) else None
        print(f"PROBE Bangkok {check_in} to {check_out}")
        print(f"  endpoint: {base_urls[0].split(':')[0]}  http status: {status}")
        print(f"  top-level keys: {sorted(payload.keys()) if isinstance(payload, dict) else type(payload).__name__}")
        if isinstance(payload, dict) and payload.get("error"):
            print(f"  error: {payload['error']}")
        if isinstance(results, list) and results:
            print(f"  results: {len(results)}")
            print("  first result:")
            for k, v in results[0].items():
                print(f"    {k}: {str(v)[:90]}")
            rates = four_star_rates(payload)
            if rates:
                print(f"  4-star hotels: {len(rates)}, median EUR {statistics.median(rates):.0f}, range {min(rates):.0f} to {max(rates):.0f}")
            else:
                print("  no usable 4-star EUR rates found in this response")
        else:
            print("  no results list in the response")
        ok = bool(results) and bool(four_star_rates(payload))
        print("PROBE OK" if ok else "PROBE DID NOT RETURN USABLE RATES")
        return 0 if ok else 1

    cities: dict[str, dict[str, Any]] = {}
    cache: dict[int, dict[str, Any] | None] = {}  # two page names can share one Agoda city (Bali)
    for name, city_id in CITY_IDS.items():
        if city_id not in cache:
            try:
                status, payload = search_city(base_urls, site_id, api_key, city_id, check_in, check_out)
            except AuthError as exc:
                print(f"ERROR: Agoda rejected the request ({exc}). Stopping.", file=sys.stderr)
                return 1
            rates = four_star_rates(payload)
            if len(rates) >= MIN_RESULTS:
                cache[city_id] = {"id": city_id, "rate": int(round(statistics.median(rates))), "n": len(rates),
                                  "low": int(round(min(rates))), "high": int(round(max(rates)))}
            else:
                cache[city_id] = None
                err = payload.get("error") if isinstance(payload, dict) else None
                print(f"  skip {name}: {len(rates)} usable rates (http {status}{', ' + str(err) if err else ''})", file=sys.stderr)
            time.sleep(0.4)
        if cache[city_id]:
            cities[name] = cache[city_id]
            print(f"  {name}: EUR {cache[city_id]['rate']} median of {cache[city_id]['n']}")

    if len(cities) < MIN_CITIES:
        print(f"\nOnly {len(cities)} cities returned rates. Aborting (hotel-data.json not overwritten).", file=sys.stderr)
        return 1

    data = {
        "meta": {
            "fetched_at": datetime.now(timezone.utc).isoformat(),
            "source": "Agoda Affiliate Lite API",
            "currency": "EUR",
            "check_in": check_in,
            "check_out": check_out,
            "nights": STAY_NIGHTS,
            "basis": "median nightly rate of the 4-star hotels among Agoda's first 30 recommended 4-star-and-up results, 2 adults, before taxes",
            "city_count": len(cities),
        },
        "cities": cities,
    }
    Path(args.out).write_text(json.dumps(data, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")
    print(f"\nWrote {len(cities)} cities to {args.out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
