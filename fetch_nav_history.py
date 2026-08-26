#!/usr/bin/env python3
"""Fetch daily NAV history for a set of mutual funds from mfapi.in and write nav_history.csv."""

import csv
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
import json

SEARCH_URL = "https://api.mfapi.in/mf/search?q={query}"
DETAIL_URL = "https://api.mfapi.in/mf/{scheme_code}"
OUTPUT_FILE = "nav_history.csv"
REQUEST_TIMEOUT = 30
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2

# Fund names to search for on mfapi.in. The first search result for each
# name is used as the matching scheme.
FUND_NAMES = [
    "SBI Bluechip Fund Direct Growth",
    "HDFC Top 100 Fund Direct Growth",
    "ICICI Prudential Bluechip Fund Direct Growth",
    "Axis Bluechip Fund Direct Growth",
    "Mirae Asset Large Cap Fund Direct Growth",
    "Nippon India Large Cap Fund Direct Growth",
    "Kotak Bluechip Fund Direct Growth",
    "UTI Nifty Index Fund Direct Growth",
    "HDFC Index Fund Sensex Plan Direct Growth",
    "ICICI Prudential Nifty Index Fund Direct Growth",
    "Parag Parikh Flexi Cap Fund Direct Growth",
    "Axis Midcap Fund Direct Growth",
    "SBI Small Cap Fund Direct Growth",
    "HDFC Mid-Cap Opportunities Fund Direct Growth",
    "Nippon India Small Cap Fund Direct Growth",
    "ICICI Prudential Value Discovery Fund Direct Growth",
    "SBI Equity Hybrid Fund Direct Growth",
    "HDFC Balanced Advantage Fund Direct Growth",
    "Axis Long Term Equity Fund Direct Growth",
    "Mirae Asset Emerging Bluechip Fund Direct Growth",
]


def fetch_json(url):
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            request = urllib.request.Request(url, headers={"User-Agent": "fetch-nav-history/1.0"})
            with urllib.request.urlopen(request, timeout=REQUEST_TIMEOUT) as response:
                return json.loads(response.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS * attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def find_scheme_code(fund_name):
    results = fetch_json(SEARCH_URL.format(query=urllib.parse.quote(fund_name)))
    if not results:
        return None
    return results[0]["schemeCode"], results[0]["schemeName"]


def fetch_nav_history(scheme_code):
    data = fetch_json(DETAIL_URL.format(scheme_code=scheme_code))
    return data.get("data", [])


def main():
    rows = []
    for fund_name in FUND_NAMES:
        print(f"Searching for: {fund_name}")
        try:
            match = find_scheme_code(fund_name)
        except RuntimeError as error:
            print(f"  ERROR searching for '{fund_name}': {error}", file=sys.stderr)
            continue

        if not match:
            print(f"  No match found for '{fund_name}'", file=sys.stderr)
            continue

        scheme_code, scheme_name = match
        print(f"  Matched scheme {scheme_code}: {scheme_name}")

        try:
            nav_records = fetch_nav_history(scheme_code)
        except RuntimeError as error:
            print(f"  ERROR fetching NAV history for {scheme_code}: {error}", file=sys.stderr)
            continue

        print(f"  Fetched {len(nav_records)} NAV records")
        for record in nav_records:
            rows.append({
                "requested_fund_name": fund_name,
                "scheme_code": scheme_code,
                "scheme_name": scheme_name,
                "date": record.get("date"),
                "nav": record.get("nav"),
            })

    if not rows:
        print("No NAV data fetched; aborting without writing output.", file=sys.stderr)
        sys.exit(1)

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as csv_file:
        writer = csv.DictWriter(
            csv_file,
            fieldnames=["requested_fund_name", "scheme_code", "scheme_name", "date", "nav"],
        )
        writer.writeheader()
        writer.writerows(rows)

    print(f"Wrote {len(rows)} rows to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
