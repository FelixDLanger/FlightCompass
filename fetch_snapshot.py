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
    TRAVELPAYOUTS_TOKEN=xxx python3 fetch_snapshot.py --probe   (test only: prints how the API
                                                                 labels airports, writes nothing)

Airport codes: the API answers with CITY codes in "origin"/"destination" (LON, TYO, NYC...)
and puts the airport in "origin_airport"/"destination_airport". The page works with airport
codes, so the snapshot stores the airports.
"""
from __future__ import annotations

import argparse
import collections
import json
import os
import re
import sys
import time
import urllib.error
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
    # SAI replaced Siem Reap's old airport REP in October 2023; the API rejects REP.
    "BLR","SGN","HAN","DPS","PNH","SAI","RGN","CMB","KTM","PEK","PVG","CAN","SZX",
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

# Airports that share a city. The API rejects a search between two of them (HTTP 400),
# so those pairs are skipped.
SAME_CITY = {
    "LHR": "LON", "LGW": "LON", "STN": "LON", "LTN": "LON", "LCY": "LON",
    "CDG": "PAR", "ORY": "PAR",
    "NRT": "TYO", "HND": "TYO",
    "KIX": "OSA", "ITM": "OSA",
    "ICN": "SEL", "GMP": "SEL",
    "PEK": "BJS", "PKX": "BJS",
    "PVG": "SHA", "SHA": "SHA",
    "IST": "IST", "SAW": "IST",
    "FCO": "ROM", "CIA": "ROM",
    "MXP": "MIL", "LIN": "MIL", "BGY": "MIL",
    "JFK": "NYC", "EWR": "NYC", "LGA": "NYC",
    "ORD": "CHI", "MDW": "CHI",
    "GRU": "SAO", "CGH": "SAO", "VCP": "SAO",
    "GIG": "RIO", "SDU": "RIO",
    "EZE": "BUE", "AEP": "BUE",
    "CGK": "JKT", "HLP": "JKT",
    "BKK": "BKK", "DMK": "BKK",
    "DXB": "DXB", "DWC": "DXB",
}

_IATA = re.compile(r"[A-Z]{3}")


def same_city(a: str, b: str) -> bool:
    return a == b or (a in SAME_CITY and SAME_CITY[a] == SAME_CITY.get(b))


def airport_code(value: Any) -> str | None:
    """A 3-letter airport code from the API, or None when the field is missing or malformed."""
    return value if isinstance(value, str) and _IATA.fullmatch(value) else None


def fetch_payload(origin: str, destination: str, month: str | None, token: str, one_way: bool = False) -> dict[str, Any]:
    """Raw API answer for one origin->destination search (raises on HTTP errors)."""
    params = {
        "origin": origin,
        "destination": destination,
        "currency": "eur",
        "sorting": "price",
        "direct": "false",
        "limit": 30,
        "one_way": "true" if one_way else "false",
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
    # Bounded retry on rate limiting (429): back off 2s, 4s, 6s, then give up on this route.
    # (The previous unbounded recursion could spin for half an hour under a long throttle.)
    payload: dict[str, Any] = {}
    for attempt in range(4):
        try:
            with urllib.request.urlopen(req, timeout=20) as resp:
                payload = json.loads(resp.read())
            break
        except urllib.error.HTTPError as exc:
            if exc.code == 429 and attempt < 3:
                time.sleep(2 * (attempt + 1))
                continue
            raise
    return payload if isinstance(payload, dict) else {}


def fetch_route(origin: str, destination: str, month: str | None, token: str, one_way: bool = False) -> list[dict[str, Any]]:
    """Fetch cheapest tickets for one origin->destination.

    Args:
        one_way: If True, fetch one-way tickets. If False (default), fetch round-trips.
    """
    if same_city(origin, destination):
        return []
    payload = fetch_payload(origin, destination, month, token, one_way)
    if not payload.get("success"):
        return []
    data = payload.get("data") or []
    if not isinstance(data, list):
        return []
    return [item for item in data if isinstance(item, dict)]


def normalise(items: list[dict[str, Any]], origin: str, destination: str) -> list[dict[str, Any]]:
    """Normalise Travelpayouts response items into our snapshot schema.

    origin/destination are the airports that were searched. The stored codes are the airports
    the fare actually uses ("origin_airport"/"destination_airport"); the API's own
    "origin"/"destination" fields hold city codes (LON, TYO...), which the page cannot place.
    """
    out = []
    for item in items:
        if not item.get("price"):
            continue
        dep = airport_code(item.get("origin_airport")) or origin
        arr = airport_code(item.get("destination_airport")) or destination
        if dep == arr:
            continue
        out.append({
            "origin": dep,
            "destination": arr,
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
        # Drop empty fields (one-way fares have no return data); the page treats a missing
        # key exactly like null, and the snapshot stays markedly smaller for mobile visitors.
        out[-1] = {k: v for k, v in out[-1].items() if v is not None}
    return out


def probe(token: str, origins: list[str], destinations: list[str]) -> int:
    """Test run: search a sample of routes and print how the API labels airports. Writes nothing.

    Pass 1 searches every destination from three origins (shows which arrival airports come back);
    pass 2 searches five destinations from every origin (shows which departure airports come back).
    """
    pairs: list[tuple[str, str]] = []
    for o in ["BKK", "LHR", "JFK"]:
        pairs += [(o, d) for d in destinations]
    for o in origins:
        pairs += [(o, d) for d in ["IST", "DXB", "BKK", "LHR", "SIN"]]
    pairs = [p for p in dict.fromkeys(pairs) if not same_city(*p)]
    print(f"PROBE {len(pairs)} origin-destination pairs, round trip and one way")

    fields: collections.Counter = collections.Counter()
    by_origin: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    by_dest: dict[str, collections.Counter] = collections.defaultdict(collections.Counter)
    city_of: dict[str, set] = collections.defaultdict(set)
    empty: dict[str, int] = collections.Counter()
    searches: dict[str, int] = collections.Counter()
    empty_sample: dict[str, str] = {}
    http_errors: list[str] = []
    items_total = no_oa = no_da = 0
    t0 = time.time()
    for o, d in pairs:
        for one_way in (False, True):
            searches[o] += 1
            try:
                payload = fetch_payload(o, d, None, token, one_way)
            except urllib.error.HTTPError as exc:
                http_errors.append(f"{o}->{d} {'OW' if one_way else 'RT'}: HTTP {exc.code}")
                continue
            except Exception as exc:  # network trouble: note it and carry on
                http_errors.append(f"{o}->{d} {'OW' if one_way else 'RT'}: {type(exc).__name__}")
                continue
            data = payload.get("data") if payload.get("success") else None
            if not isinstance(data, list) or not data:
                empty[o] += 1
                if o not in empty_sample:
                    empty_sample[o] = json.dumps({k: v for k, v in payload.items() if k != "data"})[:300] + \
                        f" data={'[]' if isinstance(data, list) else type(payload.get('data')).__name__}"
                continue
            for it in data:
                if not isinstance(it, dict):
                    continue
                items_total += 1
                fields.update(it.keys())
                oa, da = airport_code(it.get("origin_airport")), airport_code(it.get("destination_airport"))
                no_oa += oa is None
                no_da += da is None
                by_origin[o][oa or "(none)"] += 1
                by_dest[d][da or "(none)"] += 1
                if oa:
                    city_of[oa].add(str(it.get("origin")))
                if da:
                    city_of[da].add(str(it.get("destination")))
            time.sleep(0.05)

    print(f"Done in {time.time() - t0:.0f}s: {items_total} fares, {len(http_errors)} HTTP errors")
    print("Fields in each fare: " + ", ".join(f"{k} ({v})" for k, v in sorted(fields.items())))
    print(f"Fares without origin_airport: {no_oa}; without destination_airport: {no_da}")

    def show(title: str, table: dict[str, collections.Counter]) -> None:
        print(f"\n{title}")
        for code in sorted(table):
            c = table[code]
            total, own = sum(c.values()), c.get(code, 0)
            other = ", ".join(f"{k} {v}" for k, v in c.most_common() if k != code)
            print(f"  {code}: {total} fares, {own} use {code}" + (f"; others: {other}" if other else ""))

    show("Searched origin -> airports the fares depart from", by_origin)
    show("Searched destination -> airports the fares arrive at", by_dest)
    print("\nAirport -> city code the API reports for it")
    print("  " + ", ".join(f"{a}={'/'.join(sorted(c))}" for a, c in sorted(city_of.items())))
    searched = set(origins) | set(destinations)
    print("\nAirports in the answers that were not searched: " +
          (", ".join(sorted(a for a in city_of if a not in searched)) or "none"))
    print("\nOrigins with empty answers (empty / searches):")
    for o in sorted(empty, key=lambda k: -empty[k] / searches[k]):
        print(f"  {o}: {empty[o]}/{searches[o]}; first empty answer: {empty_sample.get(o, '')}")
    if http_errors:
        print("\nHTTP errors:\n  " + "\n  ".join(http_errors[:40]))
    print("\nPROBE OK" if items_total else "\nPROBE FAILED: no fares came back")
    return 0 if items_total else 1


def main() -> int:
    parser = argparse.ArgumentParser(description="Fetch FlightCompass snapshot via Travelpayouts API")
    parser.add_argument("--origin", default=None, help="Single origin IATA")
    parser.add_argument("--origins", default=None, help=f"Comma-separated (default: {','.join(DEFAULT_ORIGINS)})")
    parser.add_argument("--destinations", default=None, help="Comma-separated destinations (default: ~100 major destinations)")
    parser.add_argument("--month", default=None, help="Departure month filter: YYYY-MM (e.g. 2026-11). Default: no filter.")
    parser.add_argument("--out", default="flight-data.json")
    parser.add_argument("--token", default=None, help="Travelpayouts API token (or set TRAVELPAYOUTS_TOKEN env var)")
    parser.add_argument("--probe", action="store_true",
                        help="Test only: search a sample, print how the API labels airports, write nothing")
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

    if args.probe:
        return probe(token, origins, destinations)

    all_routes: list[dict[str, Any]] = []
    counts: dict[str, int] = {}

    for origin in origins:
        origin_count = 0
        for destination in destinations:
            if same_city(origin, destination):
                continue
            # Fetch round-trip first (default; richer for typical bookings)
            try:
                raw_rt = fetch_route(origin, destination, args.month, token, one_way=False)
                routes_rt = normalise(raw_rt, origin, destination)
                all_routes.extend(routes_rt)
                origin_count += len(routes_rt)
                time.sleep(0.05)
            except Exception as exc:
                print(f"  WARN {origin}->{destination} RT: {exc}", file=sys.stderr)
            # Fetch one-way (matters for budget SEA routes and Aviasales cache coverage)
            try:
                raw_ow = fetch_route(origin, destination, args.month, token, one_way=True)
                routes_ow = normalise(raw_ow, origin, destination)
                all_routes.extend(routes_ow)
                origin_count += len(routes_ow)
                time.sleep(0.05)
            except Exception as exc:
                print(f"  WARN {origin}->{destination} OW: {exc}", file=sys.stderr)
        counts[origin] = origin_count
        print(f"  {origin}: {origin_count} routes")

    if not all_routes:
        print("\nNo routes fetched. Aborting (snapshot not overwritten).", file=sys.stderr)
        return 1

    # The API also returns fares for nearby airports (FLL for a Miami search, EWR for New York...).
    # They are stored under their own airport code; list them so the page's airport tables can be
    # kept complete (a code the page does not know is left off the map).
    searched_o, searched_d = set(origins), set(destinations)
    extra_o = collections.Counter(r["origin"] for r in all_routes if r["origin"] not in searched_o)
    extra_d = collections.Counter(r["destination"] for r in all_routes if r["destination"] not in searched_d)
    if extra_o:
        print("Fares from nearby airports: " + ", ".join(f"{k} {v}" for k, v in extra_o.most_common()))
    if extra_d:
        print("Fares to nearby airports: " + ", ".join(f"{k} {v}" for k, v in extra_d.most_common()))

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
    # Compact JSON: ~45% smaller than indented output, so faster page loads and a lighter repo
    # (GitHub warns above 50 MB per file and rejects pushes above 100 MB).
    out_path.write_text(json.dumps(snapshot, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(f"\nWrote {len(all_routes)} routes to {out_path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
