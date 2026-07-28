"""X-Bogus signing via TikTok's own JS (window.byted_acrawler.frontierSign).

"""
from __future__ import annotations

import random
import time
from typing import Optional

from .exceptions import SigningFailed
from ._logging import get_logger
from .targets import TIKTOK_BASE

logger = get_logger("signing")

_WARMUP_URLS = [TIKTOK_BASE + "/foryou", TIKTOK_BASE, TIKTOK_BASE + "/@tiktok"]


def wait_for_acrawler(session, attempts: int = 5, deadline: Optional[float] = None) -> bool:
    """Wait until ``window.byted_acrawler.frontierSign`` is callable.

    Uses ``page.wait_for_function`` (with a randomised 5-20s timeout per
    attempt) instead of a manual evaluate-and-sleep poll. The Playwright
    helper handles greenlet/event-loop coordination internally, which
    avoids the "Cannot switch to a different thread" class of errors we
    hit with the manual loop.

    ``deadline`` (an absolute ``time.time()`` value) caps the whole wait so a
    flagged session can't burn minutes here past the caller's ``timeout=``.

    Inspired by davidteather/TikTok-Api ``generate_x_bogus``.
    """
    if deadline is not None and time.time() > deadline:
        return False
    from playwright.sync_api import TimeoutError as PWTimeoutError
    page = session.page
    expr = (
        "() => !!(window.byted_acrawler && "
        "typeof window.byted_acrawler.frontierSign === 'function')"
    )
    last_err = None
    for attempt in range(1, attempts + 1):
        if deadline is not None and time.time() > deadline:
            logger.debug("wait_for_acrawler: hit deadline before attempt %d", attempt)
            return False
        # Quick non-blocking check first — if it's already there, we're done.
        try:
            if page.evaluate(expr):
                return True
        except Exception as exc:
            last_err = exc
            logger.debug("wait_for_acrawler quick-check raised %s (attempt %d)",
                         type(exc).__name__, attempt)

        # Wait up to a random 5-15s for the function to appear, with the
        # browser handling polling internally — but never past the deadline.
        timeout_ms = random.randint(5000, 15000)
        if deadline is not None:
            remaining_ms = int((deadline - time.time()) * 1000)
            if remaining_ms <= 0:
                return False
            timeout_ms = min(timeout_ms, remaining_ms)
        try:
            page.wait_for_function(expr, timeout=max(1, timeout_ms))
            return True
        except PWTimeoutError:
            logger.debug("wait_for_acrawler: timed out after %d ms (attempt %d/%d)",
                         timeout_ms, attempt, attempts)
        except Exception as exc:
            last_err = exc
            logger.debug("wait_for_acrawler: wait_for_function raised %s (attempt %d)",
                         type(exc).__name__, attempt)

        # Warm up by visiting a known signing-loaded page before retrying —
        # skip if we're already out of time.
        if deadline is not None and time.time() > deadline:
            return False
        try:
            session.go(random.choice(_WARMUP_URLS), wait=2.0)
        except Exception as exc:
            logger.debug("warmup goto failed: %s", exc)

    if last_err is not None:
        logger.warning("wait_for_acrawler giving up; last error: %s: %s",
                       type(last_err).__name__, last_err)
    return False


def frontier_sign(session, url: str) -> str:
    """Return the X-Bogus value for url, or raise SigningFailed."""
    page = session.page
    try:
        sign = page.evaluate(
            "(u) => window.byted_acrawler && window.byted_acrawler.frontierSign(u)",
            url,
        )
    except Exception as exc:
        raise SigningFailed(f"frontierSign raised: {exc}") from exc
    if not isinstance(sign, dict):
        raise SigningFailed(f"frontierSign returned non-dict: {sign!r}")
    x_bogus = sign.get("X-Bogus", "")
    if not x_bogus:
        raise SigningFailed("frontierSign returned no X-Bogus")
    return x_bogus


def fetch_in_page(session, signed_url: str, timeout_ms: int = 30000) -> Optional[tuple]:
    """Run fetch(signed_url) inside the page context. Returns (status, body) or None."""
    page = session.page
    page.set_default_timeout(timeout_ms)
    try:
        result = page.evaluate(
            """async (u) => {
                try {
                    const r = await fetch(u, { method: 'GET', credentials: 'same-origin' });
                    const text = await r.text();
                    return { status: r.status, body: text };
                } catch (err) {
                    return { error: String(err) };
                }
            }""",
            signed_url,
        )
    except Exception as exc:
        logger.debug("fetch_in_page raised %s", exc)
        return None
    if not isinstance(result, dict) or "error" in result:
        return None
    return result.get("status"), result.get("body", "")
