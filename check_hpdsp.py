#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
hpdsp-watcher
-------------
Checks a hpdsp.net hotel-plan booking calendar (Tominoko Hotel, or any hotel
running the same "hpdsp" reservation engine) across one or more room/plan
types and one or more target months, and posts a full month-by-month status
report to Discord every run (2026-09-25 change, per your request - this is
no longer diff/event-based; every run posts the complete current picture,
whether or not anything changed since last time).

Runs standalone (no Claude, no desktop app needed) - just Python. Designed
to be triggered on a schedule by GitHub Actions (see
.github/workflows/check.yml, currently hourly to match "send Discord every
1 hour"), but you can run it anywhere (a cron job, a Raspberry Pi, your own
server) as long as the network isn't blocked.

state.json still tracks per-month "opened" history purely for the log
file's sake (so logs/hpdsp_log.md can note "just opened" the first time a
month goes live); it no longer gates what gets posted to Discord - every
run posts the full current status for every watched month, live off the
page, regardless of whether anything changed.

2026-09-25 fetch-engine change: the first live run reported every single
month as "not open" - including the CURRENT month, which was provably
wrong (a direct browser check of the exact same URL showed real,
available dates). Diagnosis (done via a browser-side `fetch()` to that
exact URL): the calendar HTML is NOT behind a login/session/cookie - a
fully cookie-less, referrer-less request from a real browser got the
correct page every time. The one thing a plain `requests.get()` can't
fake is the browser's TLS/HTTP fingerprint, and hpdsp.net is a Japanese
hotel booking engine of a kind that commonly sits behind bot-mitigation
(e.g. Akamai/Cloudflare-style WAFs) that silently serves a stripped/blank
page (still HTTP 200) to non-browser clients instead of an honest block -
which matches exactly what happened. So this script now fetches every
page with a real, headless Chromium browser (Playwright) instead of
`requests`, to present a genuine browser fingerprint. The HTML it gets
back is parsed exactly the same way as before (BeautifulSoup, see
`parse_calendar`) - only *how* the page is fetched changed.
"""

import json
import os
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

import requests  # still used for the Discord webhook POST, not for hpdsp.net
from bs4 import BeautifulSoup
from playwright.sync_api import sync_playwright


def month_range(start_year: int, start_month: int, end_year: int, end_month: int) -> list:
    """Inclusive list of (year, month) tuples from start to end."""
    months = []
    y, m = start_year, start_month
    while (y, m) <= (end_year, end_month):
        months.append((y, m))
        m += 1
        if m > 12:
            m = 1
            y += 1
    return months

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

BASE_URL = "https://www.hpdsp.net/tominoko/en/hw/hwp3200/hww3201init.do"

# Params shared by every room type at this hotel. calYear / calMonth are
# overridden per request; planCd / roomTypeCd come from each entry in
# TARGETS below.
COMMON_PARAMS = {
    "screenId": "HWW3201",
    "yadNo": "310563",
    "planListNumPlan": "5_0_0",
    "room_number": "1",
    "roomCountBkup": "1",
    "adultNumBkup": "2",
    "adultNum": "2",
    "dateUndecidedBkup": "1",
    "roomCrack": "200000",
    "roomCrackBkup": "200000",
    "roomCount": "1",
    "stayCount": "1",
    "calOpenFlg": "1",
}

# One entry per room/plan you want watched. Add as many as you like - just
# open the plan's page on hpdsp.net, copy its "planCd" and "roomTypeCd" out
# of the URL, and add a block here. "label" is only used to make Discord
# messages readable - the actual price shown in each alert is always
# scraped live from the page, never hardcoded, so it'll be correct
# regardless of what you put in "label".
#
# 2026-09-25: dropped the ¥24,200 twin-w/-terrace plan (planCd 00590035 /
# roomTypeCd 0140389) per your request - only watching the room below now.
# Add it back any time by pasting its block back in.
#
# Matching is exact on planCd + roomTypeCd (not on price - "label" below is
# just a human-readable note, it plays no part in what gets checked).
TARGETS = [
    {
        "label": "Room (planCd 00423872 / roomTypeCd 0099552)",
        "planCd": "00423872",
        "roomTypeCd": "0099552",
    },
]

# Months to watch. 2026-09-25: changed from a fixed [(2027,1), (2027,2)]
# list to "the current month through Feb 2027", per your request to also
# check the current date - this way it keeps including "now" automatically
# as time passes, you don't have to remember to update it. Bump END_MONTH
# once you no longer need Jan/Feb 2027 watched, or hardcode TARGET_MONTHS
# yourself if you'd rather have an explicit fixed list again.
END_MONTH = (2027, 2)
_today = datetime.now(timezone.utc)
TARGET_MONTHS = month_range(_today.year, _today.month, END_MONTH[0], END_MONTH[1])

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
STATE_FILE = Path(__file__).parent / "state.json"
LOG_FILE = Path(__file__).parent / "logs" / "hpdsp_log.md"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
        "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9",
}


def build_url(target: dict, year: int, month: int) -> str:
    params = dict(COMMON_PARAMS)
    params["planCd"] = target["planCd"]
    params["roomTypeCd"] = target["roomTypeCd"]
    params["calYear"] = str(year)
    params["calMonth"] = f"{month:02d}"
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{BASE_URL}?{query}"


# A single headless-Chromium instance is launched once per script run (see
# get_browser_page() / close_browser() below) and reused for every
# target/month fetch, rather than launching a fresh browser per request -
# that keeps the run fast even with 6+ page loads.
_playwright_ctx = None
_browser = None
_page = None


def get_browser_page():
    global _playwright_ctx, _browser, _page
    if _page is None:
        _playwright_ctx = sync_playwright().start()
        _browser = _playwright_ctx.chromium.launch(headless=True)
        context = _browser.new_context(
            user_agent=HEADERS["User-Agent"],
            locale="en-US",
        )
        _page = context.new_page()
    return _page


def close_browser() -> None:
    global _playwright_ctx, _browser, _page
    try:
        if _browser is not None:
            _browser.close()
    finally:
        if _playwright_ctx is not None:
            _playwright_ctx.stop()
        _browser = _page = _playwright_ctx = None


def fetch_month_html(target: dict, year: int, month: int) -> str:
    url = build_url(target, year, month)
    page = get_browser_page()
    page.goto(url, wait_until="load", timeout=30000)
    text = page.content()

    # Debug diagnostics: print a short summary of every fetch to the
    # Actions run log so a future "why is this wrong" question can be
    # answered by reading the log instead of re-diagnosing from scratch.
    print(
        f"[debug] {year:04d}-{month:02d}: GET {url}\n"
        f"[debug]   -> final_url={page.url} len={len(text)} "
        f"has_table_calender={'table_calender' in text} "
        f"has_enable={'table_calender-enable' in text} "
        f"has_reserved={'table_calender-reserved' in text}"
    )
    if "table_calender" not in text:
        # Print a chunk of the body so we can see what we actually got
        # (a captcha/interstitial page, an error page, a redirect target,
        # etc.) instead of the expected calendar markup.
        print(f"[debug]   body snippet (first 1000 chars):\n{text[:1000]}")
    return text


def parse_calendar(html: str, year: int, month: int) -> dict:
    """
    Returns {
        "opened": bool,       # is the booking window open at all for this month?
        "available": [        # list of bookable dates found
            {"date": "2027-02-16", "price": "24200", "remaining": "6"},
            ...
        ],
    }
    """
    soup = BeautifulSoup(html, "html.parser")
    table = soup.find("table", class_="table_calender")
    if table is None:
        return {"opened": False, "available": []}

    any_data = False
    available = []

    for td in table.find_all("td"):
        classes = td.get("class") or []
        number_span = td.find("span", class_="table_calender-number")
        if number_span is None:
            continue  # empty padding cell
        day_text = number_span.get_text(strip=True)
        if not day_text.isdigit():
            continue
        day = int(day_text)

        price_span = td.find("span", class_="table_calender-price")
        price_text = price_span.get_text(strip=True) if price_span else ""

        if price_text and "application period" not in price_text.lower():
            any_data = True

        if "table_calender-enable" in classes:
            any_data = True
            remaining_span = td.find("span", class_="table_calender-rest_number")
            available.append(
                {
                    "date": f"{year:04d}-{month:02d}-{day:02d}",
                    "price": re.sub(r"[^\d]", "", price_text),
                    "remaining": remaining_span.get_text(strip=True)
                    if remaining_span
                    else "",
                }
            )

    return {"opened": any_data, "available": available}


def load_state() -> dict:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text())
        except Exception:
            return {}
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.write_text(json.dumps(state, indent=2, sort_keys=True))


def log_line(text: str) -> None:
    """Append one line to the persistent, human-readable log file (kept in
    the repo, committed each run - open logs/hpdsp_log.md on GitHub any
    time to see everything this checker has ever seen, not just the alerts
    that made it to Discord)."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"- `{stamp}` {text}\n")


def format_report(results: dict) -> str:
    """
    results: {label: {(year, month): {"opened": bool, "available": [...]}}}

    Builds the Thai month-by-month report you asked for, e.g.:

        *** hpdsp ***
        September 2026
        วันที่ 16 ว่าง 6 ห้อง
        วันที่ 27 ว่าง 2 ห้อง

        October 2026

        January 2027
        -- ยังไม่เปิดจอง --
    """
    multi_target = len(results) > 1
    lines = ["*** hpdsp ***"]

    for label, months in results.items():
        if multi_target:
            lines.append(f"\n__{label}__")
        for (year, month), result in months.items():
            month_name = datetime(year, month, 1).strftime("%B %Y")
            lines.append(f"\n{month_name}")
            if not result["opened"]:
                lines.append("-- ยังไม่เปิดจอง --")
            else:
                for a in sorted(result["available"], key=lambda x: x["date"]):
                    day = int(a["date"].split("-")[2])
                    remaining = a["remaining"] or "?"
                    lines.append(f"วันที่ {day} ว่าง {remaining} ห้อง")
                # opened but nothing in "available" = fully booked -> left
                # blank under the month header, same as your example.

    return "\n".join(lines)


def notify_discord(content: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("[warn] DISCORD_WEBHOOK_URL not set - skipping Discord notification")
        print(content)
        return
    # Discord caps messages at 2000 chars; split defensively just in case
    # many targets/months fire at once.
    for i in range(0, len(content), 1900):
        chunk = content[i : i + 1900]
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": chunk}, timeout=15)
        if resp.status_code >= 300:
            print(f"[error] Discord webhook returned {resp.status_code}: {resp.text}")


def main() -> int:
    state = load_state()
    now = datetime.now(timezone.utc).isoformat()
    results = {}          # label -> {(year, month): {"opened", "available"}}
    just_opened = []      # for the log only - months that opened since last run
    checked_summaries = []

    try:
        for target in TARGETS:
            label = target["label"]
            results[label] = {}
            for year, month in TARGET_MONTHS:
                state_key = f"{target['planCd']}|{target['roomTypeCd']}|{year:04d}-{month:02d}"
                month_key = f"{year:04d}-{month:02d}"
                prev = state.get(state_key, {"opened": False})

                try:
                    html = fetch_month_html(target, year, month)
                except Exception as exc:  # Playwright raises its own
                    # exception types (TimeoutError, Error), not
                    # requests.RequestException
                    print(f"[error] fetching {label} {month_key}: {exc}")
                    # Keep last-known result in the report rather than
                    # dropping the month silently, if we have one;
                    # otherwise show closed.
                    results[label][(year, month)] = {
                        "opened": prev["opened"],
                        "available": [],
                    }
                    continue

                result = parse_calendar(html, year, month)
                results[label][(year, month)] = result

                if result["opened"] and not prev["opened"]:
                    just_opened.append(f"{label} {month_key}")

                state[state_key] = {"opened": result["opened"], "last_checked": now}

                status = "open" if result["opened"] else "not open yet"
                checked_summaries.append(
                    f"{label} {month_key}: {status}, {len(result['available'])} bookable date(s)"
                )
    finally:
        close_browser()

    save_state(state)

    report = format_report(results)

    # Log a heartbeat every run (proof it ran + what it saw), plus a
    # separate note whenever a month transitions from closed to open.
    log_line(f"checked {len(checked_summaries)} target/month combo(s): " + "; ".join(checked_summaries))
    for opened in just_opened:
        log_line(f"📅 booking window just opened: {opened}")
    log_line("posted report:\n" + report)

    notify_discord(report)
    print(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
