"""
Everything BookMyShow-specific: URLs, the in-page state readers, and matching.

All three data sets we need are structured payloads inside the page's own
Redux/query state. We never match against rendered text — BMS puts an SEO
footer listing every cinema in the city on every page, so any page-text
search reports a hit even when nothing is playing.
"""
import re
import unicodedata

from .browser import pool
from .log import log

BASE = "https://in.bookmyshow.com"

# Cities whose URL slug is not simply the slugified name.
SPECIAL_SLUGS = {
    "delhi-ncr": "national-capital-region-ncr",
    "bengaluru": "bengaluru",
}


def slugify(name: str) -> str:
    """'Rakki Cinemas: OMR, Kelambakkam' → 'rakki-cinemas-omr-kelambakkam'"""
    text = unicodedata.normalize("NFKD", name).encode("ascii", "ignore").decode()
    text = re.sub(r"[^a-zA-Z0-9]+", "-", text).strip("-").lower()
    return re.sub(r"-{2,}", "-", text)


def city_slug(city_name: str) -> str:
    base = slugify(city_name)
    return SPECIAL_SLUGS.get(base, base)


def normalize(text: str) -> str:
    """Punctuation/spacing-insensitive key for contains matching."""
    return re.sub(r"[^a-z0-9]", "", (text or "").lower())


# ─── URLs ────────────────────────────────────────────────────────────────────

def cinemas_url(slug: str) -> str:
    return f"{BASE}/{slug}/cinemas"


def venue_showtimes_url(region: str, venue_name: str, venue_code: str, date: str) -> str:
    return f"{BASE}/cinemas/{region}/{slugify(venue_name)}/buytickets/{venue_code}/{date}"


# ─── In-page readers ─────────────────────────────────────────────────────────

# The city picker only renders its full list after the region selector is
# clicked, so this runs as a "prepare" step before the reader below.
# The city picker only renders its full list after the region selector is
# clicked. That selector is a bare <span> with the city name at the top of the
# page (the page's own state does not expose the current region), so we match
# on the name we navigated to.
JS_OPEN_CITY_MODAL_TMPL = """
async () => {
    const sleep = ms => new Promise(r => setTimeout(r, ms));
    const leafWithText = (re, maxTop) => [...document.querySelectorAll('span, div, a, button, p, li, h3')]
        .find(el => {
            if (el.children.length) return false;
            if (!re.test((el.textContent || '').trim())) return false;
            if (maxTop === undefined) return true;
            const r = el.getBoundingClientRect();
            return r.top < maxTop && r.width > 0;
        });

    const selector = leafWithText(new RegExp('^' + %NAME% + '$', 'i'), 120);
    if (!selector) return 'selector-not-found';
    selector.click();
    await sleep(2500);

    // The modal shows popular cities only; expand the rest if offered.
    const expand = leafWithText(/^(View All Cities|Show all cities)$/i);
    if (expand) { expand.click(); await sleep(2500); }
    return 'opened';
}
"""


def open_city_modal_js(city_display: str) -> str:
    return JS_OPEN_CITY_MODAL_TMPL.replace("%NAME%", '"' + city_display + '"')


# Read the city picker. Scoped to the modal element — reading document text
# would sweep in the SEO footer, which lists cinemas, movies and genres.
JS_READ_CITIES = """
() => {
    const leaves = sel => [...sel.querySelectorAll('div, span, a, li, p')]
        .filter(el => el.children.length === 0);

    const popularHeading = [...document.querySelectorAll('div, span, h1, h2, h3, p')]
        .find(el => el.children.length === 0 &&
                    /^Popular Cities$/i.test((el.textContent || '').trim()));
    if (!popularHeading) return null;

    // Climb until the container holds BOTH sections (or we run out of parents).
    // Popular and Other live in sibling containers, so stopping at the first
    // roomy ancestor finds only the popular ten.
    let root = popularHeading;
    for (let i = 0; i < 10 && root.parentElement; i++) {
        root = root.parentElement;
        const t = root.innerText || '';
        if (/Other Cities/i.test(t) && root.querySelectorAll('*').length > 60) break;
        if (root.querySelectorAll('*').length > 400) break;
    }

    const texts = leaves(root).map(el => (el.textContent || '').trim());
    const otherAt = texts.findIndex(t => /^Other Cities$/i.test(t));
    const skip = /^(Popular Cities|Other Cities|Hide all cities|View All Cities|Show all cities|Detect my location|Search for your city)$/i;
    const clean = arr => [...new Set(
        arr.filter(t => t && !skip.test(t) && t.length >= 2 && t.length <= 40)
    )];

    return {
        popular: clean(otherAt < 0 ? texts : texts.slice(0, otherAt)),
        other:   otherAt < 0 ? [] : clean(texts.slice(otherAt)),
    };
}
"""

JS_READ_VENUES = """
() => {
    const st = window.__INITIAL_STATE__;
    const q = st && st.fetchVenuesListingApi && st.fetchVenuesListingApi.queries;
    if (!q) return { path: location.pathname, venues: null };
    const key = Object.keys(q).find(k => /getVenuesListingData/.test(k));
    if (!key) return { path: location.pathname, venues: null };
    const venues = q[key] && q[key].data && q[key].data.venues;
    if (!Array.isArray(venues)) return { path: location.pathname, venues: null };
    return {
        // The landing path proves which city we actually got: BookMyShow
        // redirects an unknown slug to the visitor's current region, and
        // storing those venues under the requested city would be wrong.
        path: location.pathname,
        venues: venues.map(v => ({
            name:    v.VenueName,
            code:    v.VenueCode,
            region:  v.RegionCode,
            city:    v.City,
            address: v.VenueAddress,
        })).filter(v => v.name && v.code),
    };
}
"""

# Reads the showtimes payload for one venue on one date. The query key carries
# the venue code and date, so a redirect to some other page cannot be mistaken
# for a real answer.
JS_READ_SHOWTIMES = """
({venueCode, date}) => {
    const st = window.__INITIAL_STATE__;
    const q = st && st.venueShowtimesFunctionalApi && st.venueShowtimesFunctionalApi.queries;
    if (!q) return { ok: false, reason: 'no_showtimes_state' };

    const wanted = `getShowtimesByVenue-${venueCode}-${date}`;
    const key = Object.keys(q).find(k => k === wanted) ||
                Object.keys(q).find(k => k.includes(venueCode) && k.includes(date));
    if (!key) return { ok: false, reason: 'date_not_open' };

    const data = q[key] && q[key].data;
    const events = data && data.showDetailsTransformed && data.showDetailsTransformed.Event;
    if (!Array.isArray(events) || !events.length) return { ok: false, reason: 'no_shows_listed' };

    const movies = [];
    for (const ev of events) {
        const children = Array.isArray(ev.ChildEvents) ? ev.ChildEvents : [];
        for (const child of children) {
            const shows = Array.isArray(child.ShowTimes) ? child.ShowTimes : [];
            if (!shows.length) continue;
            movies.push({
                title:    ev.EventTitle || child.EventName,
                variant:  child.EventName || '',
                language: child.EventLanguage || '',
                format:   child.EventDimension || '',
                shows:    shows.length,
                firstShow: (shows[0] && shows[0].ShowDateTime) || '',
                minPrice: (shows[0] && shows[0].MinPrice) || '',
            });
        }
    }
    if (!movies.length) return { ok: false, reason: 'no_shows_listed' };
    return { ok: true, movies };
}
"""


# ─── Async operations ────────────────────────────────────────────────────────

async def fetch_cities(seed_city: str = "chennai") -> dict:
    """
    Return {"popular": [...], "other": [...]} of city names, read from the
    city picker modal (it has no payload of its own, so we open it and read
    the rendered list).
    """
    display = seed_city.replace("-", " ").title()
    # The modal is a click away and renders lazily; right after a Cloudflare
    # clearance the page can still be settling, so give it a second go.
    for wait_ms in (4000, 9000):
        result = await pool.read_state(
            cinemas_url(seed_city),
            JS_READ_CITIES,
            prepare_js=open_city_modal_js(display),
            prepare_wait_ms=wait_ms,
        )
        if result and result.get("popular"):
            return result
        log.warning("city picker did not render (waited %sms), retrying", wait_ms)
    raise RuntimeError("city picker did not render")


async def fetch_venues(slug: str, low_priority: bool = False) -> list[dict]:
    """
    All cinemas listed for a city slug, from the venues payload.

    Returns [] for a city that genuinely has no cinemas. Raises if the page
    could not be read, or if BookMyShow bounced us to a different city.
    """
    result = await pool.read_state(cinemas_url(slug), JS_READ_VENUES,
                                   cache_key=f"venues:{slug}",
                                   low_priority=low_priority)
    if not result or result.get("venues") is None:
        raise RuntimeError(f"no venue payload for '{slug}'")

    landed = (result.get("path") or "").strip("/").split("/")[0].lower()
    if landed and landed != slug.lower():
        raise RuntimeError(f"redirected from '{slug}' to '{landed}' - not that city's venues")
    return result["venues"]


async def check_showtimes(region: str, venue_name: str, venue_code: str,
                          date: str, movie: str) -> dict:
    """
    Is `movie` playing at this venue on this date?

    Returns {'available', 'reason', 'matches': [...], 'total': int}.
    'available' is True only when a movie whose title CONTAINS the requested
    name has at least one showtime on that exact date.
    """
    payload = await pool.read_state(
        venue_showtimes_url(region, venue_name, venue_code, date),
        JS_READ_SHOWTIMES,
        {"venueCode": venue_code, "date": date},
        cache_key=f"shows:{venue_code}:{date}",
    )

    if not payload or not payload.get("ok"):
        reason = (payload or {}).get("reason", "unreadable_page")
        return {"available": False, "reason": reason, "matches": [], "total": 0}

    movies = payload["movies"]
    needle = normalize(movie)
    matches = [m for m in movies
               if needle in normalize(m["title"]) or needle in normalize(m["variant"])]

    if not matches:
        return {
            "available": False,
            "reason": "movie_not_listed",
            "matches": [],
            "total": len(movies),
            "playing": sorted({m["title"] for m in movies}),
        }

    return {
        "available": True,
        "reason": "movie_has_showtimes",
        "matches": matches,
        "total": len(movies),
    }
