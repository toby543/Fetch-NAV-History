"""Fetch daily NAV history for Nash's 20 mutual funds from mfapi.in (AMFI data).

Writes:
  nav_history.csv       fund, scheme_code, scheme_name, date (YYYY-MM-DD), nav
  nav_match_report.csv  one row per fund: which scheme was picked and whether its
                        NAV on 24-Jul-2026 matches the NAV in the Holdings export.

Scheme matching is verified, not guessed: for every fund we gather candidate
schemes from several searches, keep only plausible ones (name keywords, no
IDCW/dividend/etc.), then pick the candidate whose NAV on the reference date is
closest to the NAV in Holdings.csv. Anything off by more than 2% is flagged.
"""
import csv
import json
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime

SEARCH_URL = "https://api.mfapi.in/mf/search?q={query}"
DETAIL_URL = "https://api.mfapi.in/mf/{scheme_code}"
OUTPUT_FILE = "nav_history.csv"
REPORT_FILE = "nav_match_report.csv"
REQUEST_TIMEOUT = 30
RETRY_ATTEMPTS = 3
RETRY_DELAY_SECONDS = 2
HISTORY_FROM = date(2025, 1, 1)   # keep ~20 months so 6M / 1Y windows are covered
REF_DATE = date(2026, 7, 26)      # date of the Holdings.csv export (a Sunday)
MAX_CANDIDATES = 6                # candidate schemes to fetch per fund for verification
NAV_TOLERANCE = 0.02              # 2%

BAD_WORDS = ("idcw", "dividend", "payout", "reinvest", "bonus", "institutional",
             "segregated", "regular", "weekly", "monthly", "quarterly", "annual")

# name, search queries, must (all), anyof (at least one, optional), exclude, NAV in Holdings.csv on 26-Jul-2026
FUNDS = [
    ("SBI Focused Fund", ["SBI Focused Fund Direct Growth", "SBI Focused Equity Fund Direct Growth"],
     ["sbi", "focused"], [], [], 439.65),
    ("ICICI Pru US Bluechip Equity Fund", ["ICICI Prudential US Bluechip Equity Fund Direct Growth"],
     ["icici prudential", "us bluechip"], [], [], 85.53),
    ("ICICI Pru Large Cap Fund", ["ICICI Prudential Large Cap Fund Direct Growth", "ICICI Prudential Bluechip Fund Direct Growth"],
     ["icici prudential"], ["large cap", "bluechip"], ["us bluechip"], 118.38),
    ("Mirae Asset ELSS Tax Saver Fund", ["Mirae Asset ELSS Tax Saver Fund Direct Growth", "Mirae Asset Tax Saver Fund Direct Growth"],
     ["mirae"], ["tax saver", "elss"], [], 56.33),
    ("SBI Large Cap Fund", ["SBI Large Cap Fund Direct Growth", "SBI Bluechip Fund Direct Growth"],
     ["sbi"], ["large cap", "bluechip"], ["large mid", "large midcap"], 102.95),
    ("Nippon India ELSS Tax Saver Fund", ["Nippon India ELSS Tax Saver Fund Direct Growth", "Nippon India Tax Saver Fund Direct Growth"],
     ["nippon"], ["tax saver", "elss"], [], 129.26),
    ("Kotak Flexicap Fund", ["Kotak Flexicap Fund Direct Growth", "Kotak Flexi Cap Fund Direct Growth"],
     ["kotak"], ["flexicap", "flexi cap"], [], 95.50),
    ("Axis Focused Fund", ["Axis Focused Fund Direct Growth", "Axis Focused 25 Fund Direct Growth"],
     ["axis", "focused"], [], [], 62.41),
    ("Canara Rob Large Cap Fund", ["Canara Robeco Large Cap Fund Direct Growth", "Canara Robeco Bluechip Equity Fund Direct Growth"],
     ["canara"], ["large cap", "bluechip"], [], 70.81),
    ("HDFC Nifty 50 Index Fund", ["HDFC Nifty 50 Index Fund Direct Growth", "HDFC Index Fund Nifty 50 Plan Direct Growth"],
     ["hdfc"], ["nifty 50 plan", "nifty 50 index"], [], 232.39),
    ("HDFC Mid Cap Fund", ["HDFC Mid Cap Fund Direct Growth", "HDFC Mid-Cap Opportunities Fund Direct Growth"],
     ["hdfc", "mid cap"], [], ["index"], 227.32),
    ("UTI Children's Hybrid Fund", ["UTI Children's Hybrid Fund Growth", "UTI Children's Career Fund Savings Plan"],
     ["uti", "children"], [], [], 40.31),
    ("Parag Parikh ELSS Tax Saver Fund", ["Parag Parikh ELSS Tax Saver Fund Direct Growth", "Parag Parikh Tax Saver Fund Direct Growth"],
     ["parag parikh"], ["tax saver", "elss"], [], 31.78),
    ("Parag Parikh Flexi Cap Fund", ["Parag Parikh Flexi Cap Fund Direct Growth"],
     ["parag parikh", "flexi cap"], [], ["tax", "elss"], 89.78),
    ("Nippon India Small Cap Fund", ["Nippon India Small Cap Fund Direct Growth"],
     ["nippon", "small cap"], [], [], 200.46),
    ("SBI Contra Fund", ["SBI Contra Fund Direct Growth"],
     ["sbi", "contra"], [], [], 410.29),
    ("Nippon India Multi Cap Fund", ["Nippon India Multi Cap Fund Direct Growth"],
     ["nippon", "multi cap"], [], [], 326.93),
    ("SBI ELSS Tax Saver Fund", ["SBI ELSS Tax Saver Fund Direct Growth", "SBI Long Term Equity Fund Direct Growth"],
     ["sbi"], ["long term equity", "elss", "tax saver"], [], 469.57),
    ("JioBlackRock Flexi Cap Fund", ["JioBlackRock Flexi Cap Fund Direct Growth", "Jio BlackRock Flexi Cap Fund Direct Growth"],
     ["flexi cap"], ["jioblackrock", "jio blackrock"], [], 9.82),
    ("HDFC Focused Fund", ["HDFC Focused Fund Direct Growth", "HDFC Focused 30 Fund Direct Growth"],
     ["hdfc", "focused"], [], [], 263.17),
]


def norm(text):
    return re.sub(r"\s+", " ", re.sub(r"[^a-z0-9]+", " ", text.lower())).strip()


def fetch_json(url):
    last_error = None
    for attempt in range(1, RETRY_ATTEMPTS + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "fetch-nav-history/2.0"})
            with urllib.request.urlopen(req, timeout=REQUEST_TIMEOUT) as resp:
                return json.loads(resp.read().decode("utf-8"))
        except (urllib.error.URLError, TimeoutError, json.JSONDecodeError) as error:
            last_error = error
            if attempt < RETRY_ATTEMPTS:
                time.sleep(RETRY_DELAY_SECONDS * attempt)
    raise RuntimeError(f"Failed to fetch {url}: {last_error}")


def plausible(name, must, anyof, exclude, want_direct=True):
    n = norm(name)
    if any(b in n.split() or b in n for b in BAD_WORDS):
        return False
    if "growth" not in n:
        return False
    if want_direct and "direct" not in n:
        return False
    if not all(m in n for m in must):
        return False
    if anyof and not any(a in n for a in anyof):
        return False
    if any(x in n for x in exclude):
        return False
    return True


def candidates(queries, must, anyof, exclude, want_direct=True):
    seen = {}
    for q in queries:
        try:
            results = fetch_json(SEARCH_URL.format(query=urllib.parse.quote(q)))
        except RuntimeError as error:
            print(f"    search error for '{q}': {error}", file=sys.stderr)
            continue
        for r in results or []:
            if plausible(r["schemeName"], must, anyof, exclude, want_direct):
                seen.setdefault(r["schemeCode"], r["schemeName"])
    return list(seen.items())[:MAX_CANDIDATES]


def parse_history(records):
    out = []
    for rec in records:
        try:
            d = datetime.strptime(rec["date"], "%d-%m-%Y").date()
            out.append((d, float(rec["nav"])))
        except (KeyError, ValueError):
            continue
    out.sort()
    return out


def nav_on_or_before(history, target):
    best = None
    for d, v in history:
        if d <= target:
            best = (d, v)
        else:
            break
    return best


def pick(name, queries, must, anyof, exclude, ref_nav):
    """Return (code, scheme_name, history, nav_at_ref, pct_diff) or None."""
    cands = candidates(queries, must, anyof, exclude, want_direct=True)
    if not cands:
        # Some holdings are "Standard" (non-direct) plans; retry without requiring Direct.
        cands = candidates(queries, must, anyof, exclude, want_direct=False)
    best = None
    for code, scheme_name in cands:
        try:
            hist = parse_history(fetch_json(DETAIL_URL.format(scheme_code=code)).get("data", []))
        except RuntimeError as error:
            print(f"    history error for {code}: {error}", file=sys.stderr)
            continue
        ref = nav_on_or_before(hist, REF_DATE)
        if not ref:
            continue
        diff = abs(ref[1] - ref_nav) / ref_nav
        if best is None or diff < best[4]:
            best = (code, scheme_name, hist, ref[1], diff)
        time.sleep(0.3)
    return best


def main():
    rows, report = [], []
    for name, queries, must, anyof, exclude, ref_nav in FUNDS:
        print(f"{name}")
        match = pick(name, queries, must, anyof, exclude, ref_nav)
        if not match:
            print("  NO MATCH", file=sys.stderr)
            report.append([name, "", "", ref_nav, "", "", "NO_MATCH"])
            continue
        code, scheme_name, hist, nav_ref, diff = match
        status = "OK" if diff <= NAV_TOLERANCE else "CHECK_NAV_MISMATCH"
        print(f"  {status}: {code} {scheme_name} (NAV@ref {nav_ref} vs holdings {ref_nav}, diff {diff:.2%})")
        report.append([name, code, scheme_name, ref_nav, nav_ref, f"{diff:.4f}", status])
        for d, v in hist:
            if d >= HISTORY_FROM:
                rows.append({"fund": name, "scheme_code": code, "scheme_name": scheme_name,
                             "date": d.isoformat(), "nav": v})

    with open(REPORT_FILE, "w", newline="", encoding="utf-8") as fh:
        w = csv.writer(fh)
        w.writerow(["fund", "scheme_code", "scheme_name", "holdings_nav_26jul", "api_nav_at_ref", "rel_diff", "status"])
        w.writerows(report)

    if not rows:
        print("No NAV data fetched; aborting.", file=sys.stderr)
        sys.exit(1)

    with open(OUTPUT_FILE, "w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["fund", "scheme_code", "scheme_name", "date", "nav"])
        w.writeheader()
        w.writerows(rows)

    ok = sum(1 for r in report if r[-1] == "OK")
    print(f"Wrote {len(rows)} rows. {ok}/{len(FUNDS)} funds verified against Holdings NAV.")


if __name__ == "__main__":
    main()
