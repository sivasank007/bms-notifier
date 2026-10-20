# Ticket Watch

Watches BookMyShow for a specific movie at a specific theater on a specific
date, and **calls your phone** the moment tickets open.

```
  city + theater + movie + date  ──►  poll BookMyShow  ──►  match?  ──►  📞 call ×3
```

## Why it does not just search the page text

BookMyShow renders an SEO footer on every page listing every cinema in the
city — and the showtime grid is virtualised, so most rows are not even in the
DOM. Searching rendered text therefore reports a match when nothing is playing.

Instead, every check reads the page's own state payload
(`venueShowtimesFunctionalApi.queries["getShowtimesByVenue-<CODE>-<DATE>"]`),
which is keyed by venue code and date, and only counts movies that have at
least one entry in `ShowTimes`.

## Setup

```bash
pip install -r requirements.txt
python3 -m playwright install chromium
cp .env.example .env          # then fill in the Telnyx keys
./run.sh
```

Open http://localhost:8000.

### Telnyx (voice alerts)

`.env` already carries the credentials copied from `movie_ticket_checker.py`
(API key, caller ID `+16592446595`, connection id). `.env` is gitignored and
chmod 600 — rotate the key in the Telnyx portal if it has ever been committed
anywhere, since it is currently hard-coded in the old scripts too.

Without these three values the app still monitors and logs but places no
calls, and the form shows a warning:

```
TELNYX_API_KEY=KEY...
TELNYX_CONN_ID=...        # Call Control Application ID
TELNYX_FROM=+1...         # your Telnyx number
```

Each user types the number to be called into the form (prefilled `+91`); the
old scripts' hard-coded `TELNYX_TO` is deliberately not used, since this is
multi-user.

### Running on a server

The browser runs in the background with no window, using Chromium's *new*
headless engine (`--headless=new`), which clears Cloudflare; the legacy
headless mode is blocked outright. Nothing extra to install — no xvfb needed.

A cold profile typically needs a second warm-up attempt (~70s) before the
challenge clears; after that the clearance is stored in `data/browser-profile`
and reused. If it never clears, run once with `BMS_HEADLESS=false` and solve
it by hand in the window.

### Crawl pacing — read this before turning the dial up

Crawling all ~2,000 cities at a few seconds each **gets the IP challenged
within minutes**. That was measured, not guessed: a fast full crawl put this
machine in a Cloudflare penalty box that outlived a ten-minute cooldown, and
monitor checks failed alongside it.

So the background crawl is deliberately slow — `BMS_CATALOG_GAP_SEC=45`, about
50s per city, roughly 30 hours for a full pass. In practice:

* popular cities refresh **daily** (they are crawled first and skipped once fresh),
* the ~2,000-city long tail **rotates continuously** rather than completing daily,
* any city not yet crawled loads **on demand** the first time a user picks it.

`BMS_CATALOG_SCOPE` chooses the behaviour: `all` (default), `popular`, or `off`.
If you see repeated throttling, set it to `popular` — the on-demand path covers
the rest with no loss of function.

Two things protect the checks people are waiting on:

* the crawl runs at **low priority** and stands aside whenever a check is queued;
* it **pauses entirely** while BookMyShow is pushing back.

### When Cloudflare refuses

A clearance cookie can get burned — the profile is then refused indefinitely
while a fresh one is waved through in under a minute. After three failed
warm-ups the app rebuilds `data/browser-profile` by itself and re-warms. That is
just clearing cookies; the rate limit itself is handled by slowing down.

## How it works

| Piece | What it does |
|---|---|
| `app/catalog.py` | Crawls cities (on boot + daily) → `data/cities.json`, then theaters for **every** city → `data/theaters/<city>.json`. Popular cities go first so the app is usable in a minute; the ~2,000 others follow in the background. Cities still fresh (<24h) are skipped, so a restart resumes instead of starting over. |
| `app/browser.py` | One shared Chromium with a persistent profile. Serialises every page load, enforces a minimum gap plus jitter, backs off on challenges, and caches each venue+date result briefly. |
| `app/bms.py` | BookMyShow URLs, the in-page state readers, and punctuation-insensitive movie matching. |
| `app/monitors.py` | One asyncio task per watch: check → log → sleep → repeat. On a match it calls, then stops. |
| `app/notify.py` | Telnyx voice call, repeated `BMS_CALL_ROUNDS` times (default 3). |
| `app/main.py` | FastAPI: the JSON API plus the single-page UI. |

### One IP, many users

All crawling happens server-side, so every user's monitor leaves from the VM's
IP. Three things keep that from looking like a bot farm:

* **Dedupe** — monitors sharing a venue+date reuse one page load for
  `BMS_CACHE_TTL_SEC`. Ten users watching the same show cost one fetch.
* **Throttle** — `BMS_MIN_GAP_SEC` (default 6s) between any two loads, plus jitter.
* **Backoff** — challenges trigger exponential backoff, logged for the user as
  "BookMyShow is throttling us right now".

A 1-minute interval is offered but 5 minutes is the default; many monitors at
1 minute is the fastest way to get the VM's IP blocked.

## Data

| Path | Contents |
|---|---|
| `data/cities.json` | Every bookable city (10 popular + ~2,000 others) |
| `data/theaters/<city>.json` | Venues with codes for that city |
| `data/bmswatch.db` | Monitors and the user-facing event log (SQLite) |
| `data/browser-profile/` | Chromium profile holding the Cloudflare clearance |
| `logs/app.log` | Rotating technical log (also the last 400 lines at `/api/debug/logs`) |

## API

| Method | Path | Purpose |
|---|---|---|
| GET | `/api/cities?q=` | City search |
| GET | `/api/theaters?city=<slug>&q=` | Theater search within a city |
| POST | `/api/monitors` | Start a watch |
| GET | `/api/monitors` | This client's watches |
| POST | `/api/monitors/{id}/stop` | Stop one |
| GET | `/api/events?after=<id>` | User-facing log tail |
| GET | `/api/debug/logs` | Technical log tail |
| POST | `/api/catalog/refresh` | Force a catalog crawl |

Users are separated by a browser-generated `X-Client-Id` kept in
`localStorage` — enough to keep watch lists apart, but it is not authentication.
Anyone who knows an id can read that list, so put this behind SSO or a VPN
before exposing it beyond a trusted group.


## Deploying to Railway

```bash
railway init          # or create the project in the dashboard
railway up            # builds the Dockerfile in this repo
```

Railway detects the `Dockerfile` and `railway.toml`; nothing else to configure
for the build. Four things you must set up by hand:

**1. A volume, mounted at `/app/data`.** Without it every deploy wipes the
alerts database, the city/theater catalog and — most painfully — the browser
profile holding the Cloudflare clearance, so each restart starts from a cold
challenge.

**2. Variables** (Settings → Variables):

```
TELNYX_API_KEY=KEY...
TELNYX_CONN_ID=...
TELNYX_FROM=+1...
BMS_CATALOG_SCOPE=popular      # 'all' is ~2,000 page loads; see below
BMS_MIN_GAP_SEC=10             # be gentler than on a home connection
```

`PORT`, `TZ`, `BMS_HOST`, `BMS_HEADLESS`, `BMS_BROWSER_CHANNEL` and
`BMS_PROFILE_DIR` are already handled by the image.

**3. Memory.** Chrome plus Python needs roughly 1 GB. The smallest instance
sizes will OOM-kill the browser mid-check.

**4. Keep it awake.** The whole point is checking while you are not looking,
so the service must not sleep or scale to zero. Keep `numReplicas = 1` as
well: two instances would duplicate every check and double the load on
BookMyShow from the same IP.

### The thing most likely to bite you

Railway gives you a **datacenter IP**. Cloudflare treats those far more
harshly than a home connection — the same requests that pass from a laptop
are often challenged immediately from a cloud host. This app already does
everything a well-behaved client can (one shared browser, real Chrome, a
minimum gap between loads, exponential backoff, a self-healing profile), but
none of that changes the IP's reputation.

So deploy expecting to tune: start with `BMS_CATALOG_SCOPE=popular`, check
intervals of 15 minutes or more, and watch the Technical log tab. If every
load is blocked, the honest options are a host with a residential IP, running
the checker on a machine at home with only the UI hosted, or asking
BookMyShow about proper API access.

### Before you expose it publicly

**There is no authentication.** Anyone who finds the URL can create alerts
that place real phone calls on your Telnyx account and add load to the
crawler. Put it behind something — a password gate, Cloudflare Access, a
VPN — before sharing the link.
