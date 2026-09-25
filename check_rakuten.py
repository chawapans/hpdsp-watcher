#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
check_rakuten.py
-----------------
Uses Rakuten Travel's OFFICIAL VacantHotelSearch Web Service API to check
whether Tominoko Hotel (Rakuten hotelNo 15175) has ANY room plan available
on each date in a target range, and posts a full month-by-month status
report to Discord every run (2026-09-25 change - no longer diff/event-based,
matches check_hpdsp.py's new report style).

CAVEAT specific to this report: Rakuten's API can't tell "not open for
booking yet" apart from "sold out" (both come back as zero results), so
unlike the hpdsp report there's no "-- ยังไม่เปิดจอง --" line here - a month
with nothing listed under it just means no vacancy found, for either
reason. It also can't give a room count, only plan names, so lines read
"วันที่ 16 ว่าง (plan name)" rather than "ว่าง N ห้อง".

This is a legitimate, ToS-compliant public API (unlike scraping an OTA's
HTML), free to use with a personal Rakuten Web Service Application ID.
Get one at: https://webservice.rakuten.co.jp/  (log in with any Rakuten
account -> "アプリID発行" / "Issue Application ID" -> fill a short form ->
you get an ID immediately, no approval wait).

IMPORTANT CAVEAT: Rakuten's vacancy API works at the HOTEL level, not a
specific room/plan - it can't be filtered to "only the twin with terrace".
When this script alerts you, it means *some* plan is bookable at the hotel
for that date; open the link and confirm it's actually the room you want
before celebrating. (hpdsp/check_hpdsp.py, in contrast, watches your exact
room type directly - keep both running.)

Rate limit: Rakuten enforces ~1 request/second per Application ID. Since
2026-09-25 the watched range runs from today through END_DATE (was a fixed
~59-day window before), so the request count grows the further out
END_DATE is - e.g. ~5 months today is ~150 requests/run (~3 minutes with
the sleep below). Keep this on an HOURLY schedule (not every 15 min like
the hpdsp checker), and if you ever see 429s in the log or Rakuten quota
complaints, shorten the range or lengthen the schedule interval.
"""

import json
import os
import sys
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import requests

API_URL = "https://app.rakuten.co.jp/services/api/Travel/VacantHotelSearch/20170426"
APPLICATION_ID = os.environ.get("RAKUTEN_APP_ID", "")
HOTEL_NO = "15175"  # Tominoko Hotel's Rakuten Travel hotel ID
ADULT_NUM = 2

# Inclusive date range to watch, one night stay checked per date.
# 2026-09-25: START_DATE now tracks "today" automatically instead of a
# fixed 2027-01-01, per your request to also check the current date - so
# this keeps including "now" as time passes without needing manual edits.
# END_DATE is still fixed; bump it once you no longer need Feb 2027 watched.
START_DATE = datetime.now(timezone.utc).date()
END_DATE = date(2027, 2, 28)

DISCORD_WEBHOOK_URL = os.environ.get("DISCORD_WEBHOOK_URL", "")
STATE_FILE = Path(__file__).parent / "state_rakuten.json"
LOG_FILE = Path(__file__).parent / "logs" / "rakuten_log.md"
REQUEST_INTERVAL_SEC = 1.1  # stay under Rakuten's ~1 req/sec limit


def daterange(start: date, end: date):
    d = start
    while d <= end:
        yield d
        d += timedelta(days=1)


def extract_plan_names(hotel_entry: dict) -> list:
    """
    Best-effort extraction of plan names from a VacantHotelSearch hotel
    entry. Rakuten's JSON nests room/plan info a couple of levels deep and
    the exact shape has shifted across API versions in the past, so this
    tries a few known field paths defensively rather than assuming one.
    """
    names = []
    for room_block in hotel_entry.get("hotel", []):
        room_info = room_block.get("roomInfo")
        if not room_info:
            continue
        for room_entry in room_info:
            basic = room_entry.get("roomBasicInfo") or {}
            name = basic.get("planName") or basic.get("roomName")
            if name:
                names.append(name)
    return names


def check_date(checkin: date) -> dict:
    """Returns {"available": bool|None, "plans": [names...]} for a 1-night
    stay starting `checkin`. `available` is None on a request error (caller
    should just skip/retry next run, not treat as "no vacancy")."""
    checkout = checkin + timedelta(days=1)
    params = {
        "applicationId": APPLICATION_ID,
        "format": "json",
        "hotelNo": HOTEL_NO,
        "checkinDate": checkin.isoformat(),
        "checkoutDate": checkout.isoformat(),
        "adultNum": ADULT_NUM,
        "responseType": "middle",
    }
    resp = requests.get(API_URL, params=params, timeout=20)
    if resp.status_code == 429:
        print(f"[warn] {checkin}: rate limited (429), backing off")
        time.sleep(5)
        return {"available": None, "plans": []}
    if resp.status_code >= 400:
        print(f"[error] {checkin}: HTTP {resp.status_code}: {resp.text[:300]}")
        return {"available": None, "plans": []}

    data = resp.json()
    if data.get("error"):
        print(f"[error] {checkin}: {data.get('error')} - {data.get('error_description')}")
        return {"available": None, "plans": []}

    hotels = data.get("hotels", [])
    if not hotels:
        return {"available": False, "plans": []}

    plan_names = []
    for hotel_entry in hotels:
        plan_names.extend(extract_plan_names(hotel_entry))

    return {"available": True, "plans": plan_names}


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
    the repo, committed each run - open logs/rakuten_log.md on GitHub any
    time to see everything this checker has ever seen, not just the alerts
    that made it to Discord)."""
    LOG_FILE.parent.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    with LOG_FILE.open("a", encoding="utf-8") as f:
        f.write(f"- `{stamp}` {text}\n")


def format_report(day_results: dict) -> str:
    """
    day_results: {"YYYY-MM-DD": {"available": bool, "plans": [names...]}}

    Groups into the same month-by-month report style as check_hpdsp.py:

        *** Rakuten ***
        September 2026
        วันที่ 16 ว่าง (Standard Twin Plan)

        October 2026

    A month with nothing listed under it means no vacancy was found for
    any date in it this run - could be sold out OR not yet open for
    booking, Rakuten's API doesn't distinguish (see module docstring).
    """
    months = {}
    for key in sorted(day_results):
        year, month = int(key[:4]), int(key[5:7])
        months.setdefault((year, month), []).append(key)

    lines = ["*** Rakuten ***"]
    for (year, month), keys in months.items():
        month_name = datetime(year, month, 1).strftime("%B %Y")
        lines.append(f"\n{month_name}")
        for key in keys:
            info = day_results[key]
            if info["available"]:
                day = int(key[8:10])
                plans = info.get("plans") or []
                extra = f" ({', '.join(sorted(set(plans))[:2])})" if plans else ""
                lines.append(f"วันที่ {day} ว่าง{extra}")

    return "\n".join(lines)


def notify_discord(content: str) -> None:
    if not DISCORD_WEBHOOK_URL:
        print("[warn] DISCORD_WEBHOOK_URL not set - skipping Discord notification")
        print(content)
        return
    for i in range(0, len(content), 1900):
        chunk = content[i : i + 1900]
        resp = requests.post(DISCORD_WEBHOOK_URL, json={"content": chunk}, timeout=15)
        if resp.status_code >= 300:
            print(f"[error] Discord webhook returned {resp.status_code}: {resp.text}")


def main() -> int:
    if not APPLICATION_ID:
        print("[fatal] RAKUTEN_APP_ID environment variable not set.")
        print("Get a free one at https://webservice.rakuten.co.jp/ and add it")
        print("as a GitHub Actions secret named RAKUTEN_APP_ID.")
        return 1

    state = load_state()
    day_results = {}
    checked = 0
    available_count = 0
    errors = 0

    for i, d in enumerate(daterange(START_DATE, END_DATE)):
        if i > 0:
            time.sleep(REQUEST_INTERVAL_SEC)

        key = d.isoformat()
        result = check_date(d)

        if result["available"] is None:
            errors += 1
            # Fall back to last-known state so a transient API error for one
            # date doesn't just erase it from the report this run.
            prev = state.get(key, {})
            day_results[key] = {
                "available": prev.get("available", False),
                "plans": prev.get("plans", []),
            }
            continue

        checked += 1
        if result["available"]:
            available_count += 1

        day_results[key] = {"available": result["available"], "plans": result["plans"]}
        state[key] = {"available": result["available"], "plans": result["plans"][:5]}

    save_state(state)

    if checked == 0 and errors > 0:
        # Every single request failed - almost certainly a bad/missing
        # Application ID or Rakuten-side outage, not "no vacancy anywhere".
        # Say so plainly instead of posting a report that looks like a
        # fully-booked hotel.
        err_msg = (
            f"⚠️ **Rakuten checker failed**: all {errors} request(s) errored "
            f"this run (check RAKUTEN_APP_ID is set correctly, or Rakuten "
            f"may be having issues). See the Action's run log for details."
        )
        log_line(err_msg)
        notify_discord(err_msg)
        print(err_msg)
        return 1

    report = format_report(day_results)

    # Heartbeat line every run (proves the checker actually ran and what it
    # saw), plus the full report text, so the log is a durable copy of
    # every Discord post too.
    log_line(
        f"checked {checked} date(s) ({START_DATE}..{END_DATE}), "
        f"{available_count} with some vacancy, {errors} error(s)/skipped"
    )
    log_line("posted report:\n" + report)

    notify_discord(report)
    print(report)

    return 0


if __name__ == "__main__":
    sys.exit(main())
