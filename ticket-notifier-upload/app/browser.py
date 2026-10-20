"""
Single shared browser.

Every page load in the product goes through this module, for three reasons:

1. Cloudflare. A persistent profile keeps the clearance cookie between loads,
   so challenges become rare; headless Chromium is blocked outright, so we run
   headed (under xvfb on a server — see run.sh).
2. Politeness. One page at a time, a minimum gap between loads, plus jitter.
   Everything the server does to BookMyShow leaves from one VM IP, so the
   whole process must behave like a single ordinary browser, not a fleet.
3. Sharing. Identical targets (same venue + date) reuse one result for a
   short TTL, so ten users watching the same show cost one page load.
"""
import asyncio
import random
import shutil
import time

from playwright.async_api import async_playwright, Error as PlaywrightError

from .config import (BROWSER_CHANNEL, CACHE_TTL_SEC, CATALOG_GAP_SEC, HEADLESS,
                     MINIMIZE_WINDOW, MIN_GAP_SEC, NAV_TIMEOUT_MS, PROFILE_DIR,
                     USER_AGENT)
from .log import log


class ChallengeError(RuntimeError):
    """Cloudflare interstitial never cleared within the settle window."""


# Page is "ready" when the SPA state object exists and we are off the
# "Just a moment..." interstitial.
JS_READY = """
() => {
    const t = (document.title || '').toLowerCase();
    if (!t || t.includes('just a moment') || t.includes('attention required')) return false;
    return !!window.__INITIAL_STATE__;
}
"""


class BrowserPool:
    def __init__(self) -> None:
        self._pw = None
        self._ctx = None
        self._page = None
        self._lock = asyncio.Lock()
        self._last_load = 0.0
        self._consecutive_challenges = 0
        self._cache: dict[str, tuple[float, object]] = {}
        self._warm = False
        self._waiting_high = 0
        self._last_reset = 0.0

    # ── lifecycle ───────────────────────────────────────────────────────────
    async def start(self) -> None:
        if self._ctx:
            return
        await self._launch()
        await self.warmup()

    async def _launch(self) -> None:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._pw = await async_playwright().start()
        args = ["--disable-blink-features=AutomationControlled",
                "--no-sandbox", "--disable-dev-shm-usage"]
        if HEADLESS:
            args.insert(0, "--headless=new")

        launch = dict(headless=HEADLESS, viewport={"width": 1400, "height": 950},
                      args=args)
        if USER_AGENT:                      # empty with real Chrome - see config
            launch["user_agent"] = USER_AGENT
        try:
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(PROFILE_DIR), channel=BROWSER_CHANNEL or None, **launch)
        except PlaywrightError as exc:
            if not BROWSER_CHANNEL:
                raise
            log.warning("could not start Google Chrome (%s) - falling back to "
                        "bundled Chromium, which Cloudflare challenges harder",
                        str(exc).splitlines()[0][:100])
            self._ctx = await self._pw.chromium.launch_persistent_context(
                str(PROFILE_DIR), **launch)

        self._page = self._ctx.pages[0] if self._ctx.pages else await self._ctx.new_page()
        await self._hide_window()
        log.info("browser started (channel=%s, headless=%s, profile=%s)",
                 BROWSER_CHANNEL or "chromium", HEADLESS, PROFILE_DIR)

    async def _hide_window(self) -> None:
        """
        Minimise the browser window so a headed browser is not in the way.

        Minimised still counts as a real window to Cloudflare — verified: the
        same page that times out headless loads in about a second here.
        """
        if HEADLESS or not MINIMIZE_WINDOW:
            return
        try:
            cdp = await self._ctx.new_cdp_session(self._page)
            window_id = (await cdp.send("Browser.getWindowForTarget"))["windowId"]
            await cdp.send("Browser.setWindowBounds",
                           {"windowId": window_id, "bounds": {"windowState": "minimized"}})
        except Exception as exc:                 # not fatal: a visible window still works
            log.debug("could not minimise the browser window: %s", exc)

    async def _reset_profile(self) -> None:
        """
        Throw away the browser profile and start a clean one.

        Cloudflare can mark an individual clearance cookie as bad — when that
        happens the old profile is refused indefinitely while a fresh one is
        waved through in under a minute. This is the same thing a person does
        by clearing cookies; it is not a way around the rate limit, which is
        handled by slowing down.
        """
        log.warning("resetting browser profile - its clearance was rejected")
        try:
            if self._ctx:
                await self._ctx.close()
        except Exception:
            pass
        self._ctx = self._page = None
        shutil.rmtree(PROFILE_DIR, ignore_errors=True)
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        self._last_reset = time.monotonic()
        await self._launch()

    async def warmup(self, allow_reset: bool = True) -> None:
        """
        A cold profile gets the Cloudflare interstitial. Sit on the home page
        until it clears (it issues a clearance cookie that the persistent
        profile then keeps), reloading a couple of times if needed.

        If it will not clear, the IP is in a penalty box: there is nothing to
        do but wait, so callers get told rather than spinning.
        """
        page = self._page
        for attempt in range(1, 4):
            try:
                await page.goto("https://in.bookmyshow.com/explore/home",
                                wait_until="domcontentloaded", timeout=NAV_TIMEOUT_MS)
                await page.wait_for_function(JS_READY, timeout=60_000)
                self._warm = True
                log.info("cloudflare clearance obtained (attempt %s)", attempt)
                return
            except PlaywrightError:
                log.warning("warmup attempt %s did not clear the challenge", attempt)
                await asyncio.sleep(8)
        # A burned clearance cookie never recovers on its own; a stale profile
        # is the common cause, so rebuild it once before giving up.
        if allow_reset and (time.monotonic() - self._last_reset) > 600:
            await self._reset_profile()
            return await self.warmup(allow_reset=False)

        self._warm = False
        self._consecutive_challenges = max(self._consecutive_challenges, 3)
        log.error("warmup failed - BookMyShow is refusing us for now. Checks "
                  "keep retrying with backoff and the catalog crawl pauses "
                  "itself until it clears.")

    async def stop(self) -> None:
        try:
            if self._ctx:
                await self._ctx.close()
            if self._pw:
                await self._pw.stop()
        finally:
            self._ctx = self._page = self._pw = None

    async def _ensure_page(self):
        await self.start()
        if self._page is None or self._page.is_closed():
            self._page = await self._ctx.new_page()
        return self._page

    # ── throttling ──────────────────────────────────────────────────────────
    async def _wait_turn(self, low_priority: bool) -> None:
        gap = MIN_GAP_SEC + random.uniform(0, MIN_GAP_SEC / 2)   # jitter
        if low_priority:
            gap += CATALOG_GAP_SEC                               # crawl goes gentler
        if self._consecutive_challenges:
            # exponential backoff, capped at 5 minutes
            gap += min(300, 15 * (2 ** (self._consecutive_challenges - 1)))
        wait = (self._last_load + gap) - time.monotonic()
        if wait > 0:
            log.debug("throttle: sleeping %.1fs before next load", wait)
            await asyncio.sleep(wait)

    # ── the one public operation ────────────────────────────────────────────
    async def read_state(self, url: str, js: str, arg=None, *,
                         cache_key: str | None = None,
                         settle_sec: float = 40.0,
                         prepare_js: str | None = None,
                         prepare_wait_ms: int = 3000,
                         low_priority: bool = False):
        """
        Load `url`, wait for the SPA state to exist, then evaluate `js`
        against the page and return its result.

        `low_priority` is for the catalog crawl: it stands aside whenever a
        monitor check is waiting, and uses a wider gap. Without that, a
        multi-thousand-city crawl starves the checks people are waiting on.

        Raises ChallengeError if Cloudflare never let us through.
        """
        if cache_key:
            hit = self._cache.get(cache_key)
            if hit and (time.monotonic() - hit[0]) < CACHE_TTL_SEC:
                log.debug("cache hit %s", cache_key)
                return hit[1]

        if low_priority:
            while self._waiting_high:          # let checks jump the queue
                await asyncio.sleep(0.5)
        else:
            self._waiting_high += 1

        try:
            return await self._load(url, js, arg, cache_key, settle_sec,
                                    prepare_js, prepare_wait_ms, low_priority)
        finally:
            if not low_priority:
                self._waiting_high -= 1

    async def _load(self, url, js, arg, cache_key, settle_sec,
                    prepare_js, prepare_wait_ms, low_priority):
        async with self._lock:
            # re-check inside the lock: a queued twin may have just filled it
            if cache_key:
                hit = self._cache.get(cache_key)
                if hit and (time.monotonic() - hit[0]) < CACHE_TTL_SEC:
                    return hit[1]

            await self._wait_turn(low_priority)
            page = await self._ensure_page()
            self._last_load = time.monotonic()

            log.debug("GET %s", url)
            last_error = None
            for attempt in (1, 2):
                try:
                    if attempt == 1:
                        await page.goto(url, wait_until="domcontentloaded",
                                        timeout=NAV_TIMEOUT_MS)
                    else:
                        await asyncio.sleep(8)      # give the challenge a moment
                        await page.reload(wait_until="domcontentloaded",
                                          timeout=NAV_TIMEOUT_MS)
                    await page.wait_for_function(JS_READY, timeout=int(settle_sec * 1000))
                    last_error = None
                    break
                except PlaywrightError as exc:
                    last_error = str(exc).splitlines()[0][:200]
                    log.debug("attempt %s failed: %s", attempt, last_error)

            if last_error:
                self._consecutive_challenges += 1
                log.warning("load blocked (%s in a row): %s",
                            self._consecutive_challenges, last_error)
                # Repeated blocks mean the clearance cookie is spent — get a
                # fresh one rather than grinding through the backoff.
                if self._consecutive_challenges in (3, 6, 12):
                    log.info("re-warming the browser profile after %s blocks",
                             self._consecutive_challenges)
                    await self.warmup()
                raise ChallengeError(last_error) from None

            self._consecutive_challenges = 0

            # Some data (the city modal) only exists after an interaction.
            if prepare_js:
                try:
                    await page.evaluate(prepare_js)
                    await page.wait_for_timeout(prepare_wait_ms)
                except PlaywrightError as exc:
                    log.warning("prepare step failed: %s", str(exc).splitlines()[0][:120])

            result = await page.evaluate(js, arg) if arg is not None else await page.evaluate(js)

        if cache_key:
            self._cache[cache_key] = (time.monotonic(), result)
        return result

    @property
    def challenge_streak(self) -> int:
        return self._consecutive_challenges


pool = BrowserPool()
