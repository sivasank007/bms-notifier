"""
Voice alerts via Telnyx.

Pure outbound HTTPS — create the call, wait for pickup, speak, hang up. No
webhook server and no tunnel: Telnyx wants a webhook URL for its own event
delivery, but we never consume those events.
"""
import asyncio
import json
import os
import ssl
import time
import urllib.error
import urllib.request

from .config import (CALL_GAP_SEC, CALL_ROUNDS, TELNYX_API_KEY, TELNYX_CONN_ID,
                     TELNYX_FROM)
from .log import log

ANSWER_WAIT_SEC = 10        # time for the person to pick up
SPEAK_LOOPS     = 2         # repeats of the message within one call


def _ssl_ctx() -> ssl.SSLContext:
    try:
        import certifi
        return ssl.create_default_context(cafile=certifi.where())
    except ImportError:
        pass
    for bundle in ("/etc/ssl/cert.pem", "/etc/ssl/certs/ca-certificates.crt"):
        if os.path.exists(bundle):
            return ssl.create_default_context(cafile=bundle)
    return ssl.create_default_context()


_SSL = _ssl_ctx()


def configured() -> bool:
    return bool(TELNYX_API_KEY and TELNYX_CONN_ID and TELNYX_FROM)


def _post(url: str, body: dict, timeout: int = 15):
    req = urllib.request.Request(
        url,
        data=json.dumps(body).encode(),
        headers={"Authorization": f"Bearer {TELNYX_API_KEY}",
                 "Content-Type": "application/json"},
    )
    with urllib.request.urlopen(req, timeout=timeout, context=_SSL) as resp:
        return json.loads(resp.read() or b"{}")


def _place_call(to_number: str, message: str) -> str:
    """Blocking single call. Returns a short status string for the log."""
    try:
        data = _post("https://api.telnyx.com/v2/calls", {
            "connection_id": TELNYX_CONN_ID,
            "to": to_number,
            "from": TELNYX_FROM,
        })
        ccid = data.get("data", {}).get("call_control_id", "")
        if not ccid:
            return "call created but no call_control_id returned"
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode()[:200]
        log.error("telnyx create-call HTTP %s: %s", exc.code, detail)
        return f"call failed (HTTP {exc.code})"
    except Exception as exc:
        log.error("telnyx create-call error: %s", exc)
        return f"call failed ({type(exc).__name__})"

    time.sleep(ANSWER_WAIT_SEC)

    try:
        _post(f"https://api.telnyx.com/v2/calls/{ccid}/actions/speak", {
            "payload": message, "voice": "female", "language": "en-US",
            "loop": SPEAK_LOOPS,
        })
    except Exception as exc:
        log.warning("telnyx speak failed: %s", exc)

    time.sleep(SPEAK_LOOPS * 14)
    try:
        _post(f"https://api.telnyx.com/v2/calls/{ccid}/actions/hangup", {})
    except Exception:
        pass
    return "call completed"


def build_message(movie: str, venue: str, date_display: str, shows: int) -> str:
    return (f"Attention. Tickets are now open for {movie} at {venue} on {date_display}. "
            f"{shows} shows are listed. Please book immediately.")


async def alert(to_number: str, message: str, on_log) -> None:
    """
    Ring `to_number` CALL_ROUNDS times, reporting each round through `on_log`.
    Runs the blocking HTTP/sleep work in a worker thread.
    """
    if not configured():
        on_log("Telnyx is not configured - no call placed. Set TELNYX_API_KEY, "
               "TELNYX_CONN_ID and TELNYX_FROM.", "warn")
        return

    for round_no in range(1, CALL_ROUNDS + 1):
        on_log(f"Calling {to_number} (attempt {round_no} of {CALL_ROUNDS})…", "info")
        status = await asyncio.to_thread(_place_call, to_number, message)
        on_log(f"Attempt {round_no}: {status}.", "info")
        if round_no < CALL_ROUNDS:
            await asyncio.sleep(CALL_GAP_SEC)
    on_log("All call attempts finished.", "success")
