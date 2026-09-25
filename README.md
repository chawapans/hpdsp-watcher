# hpdsp-watcher

Watches Tominoko Hotel (Fujikawaguchiko, Japan) vacancy, from today through
Feb 2027, across **two channels**, and posts a full month-by-month status
report to a Discord channel **every hour** (2026-09-25: changed from
15-min/event-only alerts to an hourly full-report per your request). Runs
entirely on GitHub's free servers — **your computer and Claude do not need
to be open at all.**

Two independent checkers, because different booking channels can get
different room allocations from the hotel and open them at different
times:

| Checker | Channel | What it watches | Schedule |
|---|---|---|---|
| `check_hpdsp.py` | Hotel's own direct-booking site (hpdsp.net) | 1 specific room/plan type, exact vacancy + room count | hourly, at :00 |
| `check_rakuten.py` | Rakuten Travel (official API) | hotel-level vacancy (any plan) | hourly, at :30 |

### What the Discord messages look like

Every run posts the **complete current picture** for every watched month,
not just what's new — open the channel any time and you see everything at
a glance:

```
*** hpdsp ***

September 2026
วันที่ 16 ว่าง 6 ห้อง
วันที่ 27 ว่าง 2 ห้อง

October 2026

November 2026

December 2026

January 2027
-- ยังไม่เปิดจอง --

February 2027
```

A blank line under a month means "checked, fully booked" (like
October–December above); `-- ยังไม่เปิดจอง --` means the booking window for
that month isn't even open yet. `check_rakuten.py` posts the same
month-by-month shape under a `*** Rakuten ***` header, except its lines
show plan names instead of a room count (`วันที่ 16 ว่าง (Standard Twin
Plan)`), and it can't show `-- ยังไม่เปิดจอง --` at all — Rakuten's API
can't tell "not open yet" apart from "sold out", so a blank month there
just means no vacancy was found for either reason (see the caveat in
`check_rakuten.py`'s docstring).

Room type currently watched by `check_hpdsp.py` (edit `TARGETS` there to change):

- **planCd `00423872` / roomTypeCd `0099552`** — matching is exact on
  these two codes, not on price. (The `"label"` in `check_hpdsp.py` is just
  a human-readable note for Discord messages — rename it to whatever this
  room is actually called if you like; I couldn't load the page to read
  its name off, browser access was declined. The price shown in each
  Discord alert is always the real, live price scraped from the page at
  check time, never guessed or hardcoded.)

  *Dropped 2026-09-25:* the Top floor twin w/ terrace plan (planCd
  `00590035` / roomTypeCd `0140389`, ¥24,200/night) — you said you don't
  want that one anymore. Its config block is still in the file history if
  you ever want it back; just re-add an entry to `TARGETS`.

**Why not also watch Jalan directly?** hpdsp.net's `yadNo=310563` is the
same ID Jalan uses for this hotel (`jalan.net/yad310563/`), which strongly
suggests the "direct" site and Jalan share the same underlying inventory —
so `check_hpdsp.py` likely already reflects Jalan's availability. On top
of that, Jalan's own public API stopped accepting new developer signups in
February 2020, so the only way to check it programmatically now is
scraping its HTML, which is a ToS grey area I didn't want to build in by
default. Say the word if you'd rather have that anyway and I'll add it
with that caveat clearly flagged.

## How it works

**`check_hpdsp.py`** sends a plain HTTP GET to the same URL your browser
uses (no login/session needed — verified this works), for every
combination of room type (`TARGETS`) and month (`TARGET_MONTHS`, computed
automatically as "the current month through `END_MONTH`" so it always
includes today's month without manual edits). It parses each month's
calendar table into an "opened?" flag plus a list of bookable dates with
room counts, formats the whole thing into the report shown above, and
posts that **every run, unconditionally** — there's no "only if something
changed" gate anymore. `state.json` still exists, but now purely so the
log file (`logs/hpdsp_log.md`) can note the moment a month flips from
closed to open; it no longer decides what gets posted to Discord.

**`check_rakuten.py`** calls Rakuten Travel's official `VacantHotelSearch`
API once per date in the watched range (`START_DATE`, i.e. today, through
`END_DATE`), asking "is anything bookable at hotelNo 15175 for a 1-night
stay starting on this date?". It's a legitimate public API (see "Adding
Rakuten Travel" below for signup), not scraping — but the trade-off is it
can only tell you *some* plan is open at the hotel, not specifically your
target room, and it can't give a room count. Treat a Rakuten line as "go
check by hand", and an hpdsp line as "that exact room, that many rooms,
bookable right now". Same always-post-a-full-report approach as
`check_hpdsp.py`; if every single request in a run errors out (bad
`RAKUTEN_APP_ID`, Rakuten outage), it posts a plain-language failure
notice instead of a misleading "nothing available anywhere" report.

## Logs

Both checkers append to a plain-text log every single run so you have
proof they're actually running and a durable history that outlives
Discord:

- `logs/hpdsp_log.md` — one heartbeat line per run ("checked 6
  target/month combo(s): ... not open yet ... open, 2 bookable date(s)"),
  a note whenever a month flips from closed to open, and a copy of the
  full report text posted to Discord that run.
- `logs/rakuten_log.md` — same idea ("checked 157 date(s)
  (2026-09-25..2027-02-28), 2 with some vacancy, 0 errors") plus its own
  copy of each posted report.

Open either file straight on GitHub (it renders as a bulleted Markdown
list) any time you want to see exactly what the bot has seen, or `git pull`
to read it locally. Nothing is ever deleted from these — they only grow,
and now every run's full report gets logged (not just events), so they'll
grow faster than before — so if a file ever gets uncomfortably large, feel
free to delete old lines by hand (or ask me to add automatic
trimming/rotation).

## One-time setup (about 5 minutes)

1. **Create a Discord webhook** (skip if you already have one):
   In your Discord server → the channel you want alerts in → gear icon
   (Edit Channel) → Integrations → Webhooks → New Webhook → Copy Webhook
   URL.

2. **Create a new GitHub repository** (a free account works fine; make it
   private if you'd rather the URL pattern not be public):
   - github.com → New repository → any name, e.g. `hpdsp-watcher`.
   - Upload **everything in this folder** (`check_hpdsp.py`,
     `check_rakuten.py`, `.github/workflows/` — both workflow files,
     `tests/`, `.gitignore`, this `README.md`) to the repo — either
     drag-and-drop in the GitHub web UI, or via git:
     ```
     git init
     git add .
     git commit -m "Initial watcher"
     git branch -M main
     git remote add origin https://github.com/<you>/hpdsp-watcher.git
     git push -u origin main
     ```

3. **Add the webhook as a secret**: repo → Settings → Secrets and
   variables → Actions → New repository secret →
   name it `DISCORD_WEBHOOK_URL`, paste the URL from step 1.

4. **Enable Actions** (usually on by default for a new repo): repo →
   Actions tab → if prompted, click "I understand my workflows, enable
   them".

5. Test it immediately instead of waiting up to an hour: Actions tab →
   "Check hpdsp hotel availability" → Run workflow. Check the run's log,
   and check your Discord channel — you should see a full report post
   within a minute or two.

That's it — from then on GitHub fires this every hour on its own
infrastructure, forever, for free (well within GitHub's free-tier Actions
minutes for a public repo, and comfortably within the free private-repo
minutes too since each run takes well under a minute).

## Adding Rakuten Travel (optional, ~3 more minutes)

1. Go to **webservice.rakuten.co.jp**, log in with any Rakuten account
   (create one free if you don't have one).
2. Find "アプリID発行" / "Application ID Issuance" → fill in a short form
   (app name, one-line purpose like "personal hotel vacancy checker") →
   you get an **Application ID** immediately, no approval wait.
3. Add it as another repo secret: Settings → Secrets and variables →
   Actions → New repository secret → name it `RAKUTEN_APP_ID` → paste the
   ID.
4. That's it — `check-rakuten.yml` is already in this repo and will start
   firing hourly (at :30) once the secret exists. Test it the same way:
   Actions tab → "Check Rakuten Travel availability" → Run workflow.

Rakuten enforces roughly 1 request/second per Application ID, and this
script makes one request per day in the watched range (today through
END_DATE) — at today's date that's ~150+ requests/run (~3 minutes), which
is why it's hourly rather than more frequent. Don't shorten that interval
without also shrinking `END_DATE` in `check_rakuten.py`.

## Customizing

- **Add/remove room types**: edit the `TARGETS` list in `check_hpdsp.py`.
  Each entry just needs a `label` (any text, used in Discord messages),
  `planCd`, and `roomTypeCd` — copy those two codes out of the room's
  plan-detail URL on hpdsp.net. `yadNo` and the rest are shared across all
  targets in `COMMON_PARAMS` since they're all the same hotel; if you ever
  point this at a *different* hotel, update `yadNo` there too.
- **Different months**: `check_hpdsp.py` watches "current month through
  `END_MONTH`" automatically — just change `END_MONTH` (currently
  `(2027, 2)`) if you need further out. Each month is checked against
  every target in `TARGETS`, so more of either multiplies requests/run.
- **Check more/less often**: edit the cron line in
  `.github/workflows/check.yml` (`0 * * * *` = hourly, on the hour;
  GitHub's practical minimum is about every 5 minutes, e.g. `*/5 * * * *`
  — remember more frequent = more Discord messages, since every run posts
  a full report now) or `check-rakuten.yml` (`30 * * * *` = hourly, at :30
  — see the rate-limit note above before going more frequent).
- **Rakuten date range**: `check_rakuten.py` watches "today through
  `END_DATE`" automatically — edit `END_DATE` to change how far out it
  looks. A narrower range means fewer, faster API calls per run.

## Why not have Claude run this directly?

Claude's cloud sandbox can only reach an allow-listed set of domains, and
hpdsp.net isn't on it (confirmed: outbound requests get rejected by the
sandbox's network policy). The only way Claude itself could check this
site was by driving a real browser on your computer — which meant your
computer and the Claude desktop app had to be open and online at check
time. Running the checker as a GitHub Action sidesteps that entirely: it's
plain Python making a normal HTTP request from GitHub's own servers, with
nothing depending on your machine being on.
