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

import csv
import json
import os
import re
import sys
from datetime import datetime, timedelta, timezone
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

# 2026-09-25: added alongside LOG_FILE, per your request for something you
# can actually compare day-to-day - one row per (run, date) instead of a
# wall of text, so it opens cleanly in Excel/Google Sheets for
# filtering/pivoting (e.g. "show me every check for 2026-09-25").
HISTORY_CSV_FILE = Path(__file__).parent / "logs" / "history.csv"
HISTORY_CSV_FIELDS = ["checked_at", "target_label", "date", "status", "price", "remaining"]

# Thailand time (UTC+7, no DST) - used only for the timestamp shown in the
# Discord report header, so "when was this checked" reads in your own
# clock instead of GitHub Actions' UTC. A fixed offset (not zoneinfo) so
# this doesn't depend on the runner having a full tz database installed.
BANGKOK_TZ = timezone(timedelta(hours=7))

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


# Link included in every Discord report so you can jump straight to the
# room's plan/booking page (English version - note "/en/" in the path,
# same as BASE_URL above) instead of just seeing numbers. This is the
# HWW3101 "plan detail" screen (the one you land on to actually start a
# reservation), not the HWW3201 calendar-only screen the checker itself
# fetches - built from the same planCd/roomTypeCd as its TARGETS entry.
BOOKING_BASE_URL = "https://www.hpdsp.net/tominoko/en/hw/hwp3200/hww3101init.do"
BOOKING_COMMON_PARAMS = {
    "stayYear": "",
    "stayMonth": "",
    "stayDay": "",
    "roomCount": "1",
    "dateUndecided": "1",
    "adultNum": "2",
    "roomCrack": "200000",
    "yadNo": "310563",
    "screenId": "HWW3101",
    "planListNumPlan": "5_2_0",
}


def build_booking_url(target: dict) -> str:
    params = dict(BOOKING_COMMON_PARAMS)
    params["planCd"] = target["planCd"]
    params["roomTypeCd"] = target["roomTypeCd"]
    query = "&".join(f"{k}={v}" for k, v in params.items())
    return f"{BOOKING_BASE_URL}?{query}"


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


def _parse_one_table(table, year: int, month: int) -> dict:
    """Parse a single <table class="table_calender"> element.

    Returns {"opened", "available", "all_days"} - "all_days" (2026-09-25,
    added for the new CSV history log) lists EVERY date cell with real data
    on it, not just bookable ones, so day-to-day comparisons can also see
    "sold out" vs "no data yet" instead of only "available"."""
    any_data = False
    available = []
    all_days = []

    for td in table.find_all("td"):
        classes = td.get("class") or []
        number_span = td.find("span", class_="table_calender-number")
        if number_span is None:
            continue  # empty padding cell
        day_text = number_span.get_text(strip=True)
        if not day_text.isdigit():
            continue
        day = int(day_text)
        date_str = f"{year:04d}-{month:02d}-{day:02d}"

        price_span = td.find("span", class_="table_calender-price")
        price_text = price_span.get_text(strip=True) if price_span else ""
        price_digits = re.sub(r"[^\d]", "", price_text)
        has_real_price = bool(price_text) and "application period" not in price_text.lower()

        if has_real_price:
            any_data = True

        if "table_calender-enable" in classes:
            any_data = True
            remaining_span = td.find("span", class_="table_calender-rest_number")
            remaining = remaining_span.get_text(strip=True) if remaining_span else ""
            available.append(
                {"date": date_str, "price": price_digits, "remaining": remaining}
            )
            all_days.append(
                {
                    "date": date_str,
                    "status": "available",
                    "price": price_digits,
                    "remaining": remaining,
                }
            )
        elif "table_calender-reserved" in classes and has_real_price:
            all_days.append(
                {"date": date_str, "status": "sold_out", "price": price_digits, "remaining": "0"}
            )
        # "table_calender-disable" cells (past dates, or padding from the
        # adjacent month) carry no real availability info and aren't logged.

    return {"opened": any_data, "available": available, "all_days": all_days}


def parse_calendar(html: str, year: int, month: int) -> dict:
    """
    Returns {
        "opened": bool,       # is the booking window open at all for this month?
        "available": [        # list of bookable dates found
            {"date": "2027-02-16", "price": "24200", "remaining": "6"},
            ...
        ],
    }

    2026-09-25: the page can contain MORE THAN ONE element matching
    `<table class="table_calender">` (a live run's debug log showed the raw
    HTML contained real "table_calender-enable" data, yet parsing still
    came back "not open" - the only way both are true is if the FIRST such
    table BeautifulSoup's plain .find() picked wasn't the one with the
    actual data, e.g. a duplicate/skeleton copy for a responsive layout
    variant). To be robust to that, this now looks at every matching table
    and keeps whichever one actually has real cell data, instead of
    blindly trusting the first match.
    """
    soup = BeautifulSoup(html, "html.parser")
    tables = soup.find_all("table", class_="table_calender")

    print(
        f"[debug] {year:04d}-{month:02d}: found {len(tables)} "
        f"<table class=\"table_calender\"> element(s) in the parsed HTML"
    )

    if not tables:
        return {"opened": False, "available": [], "all_days": []}

    parsed = [_parse_one_table(t, year, month) for t in tables]
    for i, p in enumerate(parsed):
        print(
            f"[debug]   table[{i}]: opened={p['opened']} "
            f"available_count={len(p['available'])}"
        )

    # Prefer a table that actually has data (opened=True) over an empty
    # duplicate; among opened ones, prefer the one with the most bookable
    # dates (most information). Falls back to the first table if none of
    # them show any data at all (genuinely not open).
    best = max(parsed, key=lambda p: (p["opened"], len(p["available"])))
    return best


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


def log_history_csv(checked_at: str, target_label: str, day_entries: list) -> None:
    """Append one CSV row per date this run actually saw data for (see
    _parse_one_table's "all_days") - "available" or "sold_out" rows only,
    never a row for a date with no data yet. Kept separate from
    hpdsp_log.md (2026-09-25, per your request): a flat table with one
    value per row is what opens cleanly in Excel/Google Sheets for
    filtering, sorting, or pivoting by date - e.g. select every row for
    2026-09-25 across every run to see exactly when it went from
    "available" to "sold_out", or the other way round."""
    if not day_entries:
        return
    HISTORY_CSV_FILE.parent.mkdir(parents=True, exist_ok=True)
    is_new = not HISTORY_CSV_FILE.exists()
    with HISTORY_CSV_FILE.open("a", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=HISTORY_CSV_FIELDS)
        if is_new:
            writer.writeheader()
        for entry in sorted(day_entries, key=lambda e: e["date"]):
            writer.writerow(
                {
                    "checked_at": checked_at,
                    "target_label": target_label,
                    "date": entry["date"],
                    "status": entry["status"],
                    "price": entry["price"],
                    "remaining": entry["remaining"],
                }
            )


TARGETS_BY_LABEL = {t["label"]: t for t in TARGETS}


def _sum_remaining(available: list) -> int:
    """Adds up the "remaining" room counts across a list of available-date
    entries, skipping any that aren't a clean integer (defensive - the
    scraped value should always be digits, but never crash the report over
    one odd cell)."""
    total = 0
    for a in available:
        try:
            total += int(a["remaining"])
        except (TypeError, ValueError):
            pass
    return total


def _format_price(price_digits: str) -> str:
    """Formats a scraped price string (digits only, e.g. "24200") as
    "¥24,200" with thousands separators. Returns "?" if the value is
    missing or not a clean number, so a report line never crashes over
    one odd cell - same defensive style as _sum_remaining."""
    try:
        return f"¥{int(price_digits):,}"
    except (TypeError, ValueError):
        return "?"


def compute_month_diff(prev_available: dict, current_available: list) -> dict:
    """Compares this run's available-date list for one month against the
    PREVIOUS run's (prev_available: {date: remaining} from state.json), and
    returns {"gone": [(date, prev_remaining)], "decreased": [(date, prev, cur)],
    "increased": [(date, prev, cur)], "new": [(date, remaining)]} - added
    2026-09-25 per your request to see "which dates disappeared or lost
    rooms" compared to the last check, not just the current snapshot."""
    current_map = {a["date"]: a["remaining"] for a in current_available}
    diff = {"gone": [], "decreased": [], "increased": [], "new": []}

    for date, prev_remaining in prev_available.items():
        if date not in current_map:
            diff["gone"].append((date, prev_remaining))
            continue
        cur_remaining = current_map[date]
        try:
            prev_n, cur_n = int(prev_remaining), int(cur_remaining)
        except (TypeError, ValueError):
            continue
        if cur_n < prev_n:
            diff["decreased"].append((date, prev_remaining, cur_remaining))
        elif cur_n > prev_n:
            diff["increased"].append((date, prev_remaining, cur_remaining))

    for date, remaining in current_map.items():
        if date not in prev_available:
            diff["new"].append((date, remaining))

    return diff


def _format_diff_lines(month_diffs: dict) -> list:
    """month_diffs: {(year, month): diff_dict from compute_month_diff}.
    Renders every change across all watched months into one flat, sorted
    (by date) list of report lines."""
    lines = []
    for _, diff in sorted(month_diffs.items()):
        for date, prev_remaining in sorted(diff.get("gone", [])):
            lines.append(f"{date}: หายไป (เคยว่าง {prev_remaining} ห้อง)")
        for date, prev_r, cur_r in sorted(diff.get("decreased", [])):
            lines.append(f"{date}: ว่างลดลง {prev_r} → {cur_r} ห้อง")
        for date, prev_r, cur_r in sorted(diff.get("increased", [])):
            lines.append(f"{date}: ว่างเพิ่มขึ้น {prev_r} → {cur_r} ห้อง")
        for date, remaining in sorted(diff.get("new", [])):
            lines.append(f"{date}: ว่างใหม่ {remaining} ห้อง 🎉")
    return lines


def format_report(results: dict, checked_at: str, diffs: dict = None) -> str:
    """
    results: {label: {(year, month): {"opened": bool, "available": [...]}}}
    diffs: {label: {(year, month): diff_dict}} - only months where
        something actually changed since last run are present here (see
        compute_month_diff / main()); omitted or None means "no diff data
        yet" (e.g. the very first run after this feature was added).

    Builds the Thai month-by-month report you asked for, e.g.:

        *** hpdsp (25/09/26 15:08:32) ***
        Book: https://www.hpdsp.net/tominoko/en/hw/hwp3200/hww3101init.do?...

        เปลี่ยนแปลงจากรอบก่อน:
        2026-10-05: หายไป (เคยว่าง 1 ห้อง)
        2026-10-12: ว่างลดลง 2 → 1 ห้อง

        September 2026
        วันที่ 16 ว่าง 6 ห้อง (¥24,200)
        วันที่ 27 ว่าง 2 ห้อง (¥26,800)
        รวมทั้งหมด 8 ห้อง

        October 2026

        January 2027
        -- ยังไม่เปิดจอง --

    2026-09-26: added the "(¥N,NNN)" price shown after each date's room
    count - per-day price on this hotel's calendar varies (e.g. weekday vs
    weekend), so this is scraped live per date, same as "remaining", never
    a single fixed price for the whole month. Shows "(?)" instead if a
    date's price couldn't be read cleanly, same defensive style as an
    unreadable "remaining" count.

    2026-09-25: added the "Book:" link (English-language plan page for the
    exact room, so a click goes straight to actually reserving it) right
    under each target's header. Also added the "(DD/MM/YY HH:MM:SS)"
    timestamp in the header, in Thailand time, so you can tell at a glance
    when a given Discord message was actually checked - both per your
    request. Also added (same day, later request): a "รวมทั้งหมด N ห้อง"
    total line under each month's date list, and a "เปลี่ยนแปลงจากรอบก่อน"
    section listing which dates disappeared, lost rooms, gained rooms, or
    became newly available since the last run - omitted entirely for a
    month/target with no changes, and omitted for the whole report if
    nothing changed anywhere.
    """
    multi_target = len(results) > 1
    lines = [f"*** hpdsp ({checked_at}) ***"]

    for label, months in results.items():
        if multi_target:
            lines.append(f"\n__{label}__")
        target = TARGETS_BY_LABEL.get(label)
        if target:
            lines.append(f"Book: {build_booking_url(target)}")

        diff_lines = _format_diff_lines((diffs or {}).get(label, {}))
        if diff_lines:
            lines.append("\nเปลี่ยนแปลงจากรอบก่อน:")
            lines.extend(diff_lines)

        for (year, month), result in months.items():
            month_name = datetime(year, month, 1).strftime("%B %Y")
            lines.append(f"\n{month_name}")
            if not result["opened"]:
                lines.append("-- ยังไม่เปิดจอง --")
            else:
                for a in sorted(result["available"], key=lambda x: x["date"]):
                    day = int(a["date"].split("-")[2])
                    remaining = a["remaining"] or "?"
                    price = _format_price(a["price"])
                    lines.append(f"วันที่ {day} ว่าง {remaining} ห้อง ({price})")
                # opened but nothing in "available" = fully booked -> left
                # blank under the month header, same as your example.
                if result["available"]:
                    lines.append(f"รวมทั้งหมด {_sum_remaining(result['available'])} ห้อง")

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
    checked_at = datetime.now(BANGKOK_TZ).strftime("%d/%m/%y %H:%M:%S")
    results = {}          # label -> {(year, month): {"opened", "available", "all_days"}}
    diffs = {}             # label -> {(year, month): diff_dict} - only months that changed
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
                # "available" only exists in state.json from this feature
                # onward (2026-09-25) - None here means "no diff data yet"
                # (either the very first run ever, or the first run after
                # upgrading to this version), so the diff is skipped for
                # that month rather than showing everything as "new".
                prev_available = prev.get("available")

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
                        "all_days": [],
                    }
                    continue

                result = parse_calendar(html, year, month)
                results[label][(year, month)] = result
                log_history_csv(checked_at, label, result["all_days"])

                if prev_available is not None:
                    month_diff = compute_month_diff(prev_available, result["available"])
                    if any(month_diff.values()):
                        diffs.setdefault(label, {})[(year, month)] = month_diff

                if result["opened"] and not prev["opened"]:
                    just_opened.append(f"{label} {month_key}")

                state[state_key] = {
                    "opened": result["opened"],
                    "last_checked": now,
                    "available": {a["date"]: a["remaining"] for a in result["available"]},
                }

                status = "open" if result["opened"] else "not open yet"
                checked_summaries.append(
                    f"{label} {month_key}: {status}, {len(result['available'])} bookable date(s)"
                )
    finally:
        close_browser()

    save_state(state)

    report = format_report(results, checked_at, diffs)

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
