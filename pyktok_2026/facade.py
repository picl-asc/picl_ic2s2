"""Module-level facade — the public API of pyktok_2026.

All functions live here and route to a singleton (DOMEngine, APIEngine) pair
that share one BrowserSession.
"""
from __future__ import annotations

import os
import random
import time
from typing import Any, Dict, List, Optional

import pandas as pd

from ._browser import BrowserSession
from ._csv import (
    COMMENT_EXPORT_COLUMNS,
    comment_export_row,
    deduplicate,
    default_endpoint_filename,
    default_multi_page_metadata_filename,
    generate_data_row,
    safe,
)
from ._dispatcher import jupyter_safe, shutdown as _dispatcher_shutdown
from ._logging import get_logger, set_verbosity as _set_verbosity
from ._setup import setup as _setup
from .api import APIEngine
from .dom import DOMEngine
from .exceptions import PyktokError, SetupRequired, SigningFailed, Throttled
from .targets import TIKTOK_BASE, normalize_hashtag, normalize_username

logger = get_logger("facade")


# ---- mode resolution ------------------------------------------------------
_VALID_MODES = ("api", "dom", "tikwm", "auto")
_VALID_VIDEO_METHODS = ("dom", "tikwm", "api")


def _resolve_mode(
    mode: Optional[str],
    use_api: bool,
    supported: tuple,
    fn_name: str = "",
) -> str:
    """Resolve mode= / use_api= into one of {api,dom,tikwm}.

    - If mode is None/empty, derive from use_api (True -> 'api', False -> 'dom').
    - If mode is set, it wins (use_api is ignored, no error).
    - If the resolved mode isn't in `supported`, raise ValueError listing options.
    """
    if mode is None or mode == "":
        mode = "api" if use_api else "dom"
    mode = mode.lower()
    if mode not in _VALID_MODES:
        raise ValueError(
            f"{fn_name}: mode={mode!r} is not one of {_VALID_MODES}. "
            f"(Supported here: {sorted(supported)})"
        )
    if mode == "auto":
        # 'auto' (try api, fall back to tikwm) is opt-in per function: only the
        # read getters that have an explicit auto branch list it in `supported`.
        # Functions without it raise a clear error rather than silently acting
        # as 'api'.
        if "auto" in supported:
            return "auto"
        raise ValueError(
            f"{fn_name}: mode='auto' is not supported here. Try {sorted(s for s in supported if s != 'auto')}."
        )
    if mode not in supported:
        raise ValueError(
            f"{fn_name}: mode={mode!r} is not supported. "
            f"Try one of {sorted(supported)}."
        )
    return mode


def _resolve_video_method(method: Optional[str]) -> str:
    method = (method or "dom").lower()
    if method not in _VALID_VIDEO_METHODS:
        raise ValueError(
            f"video_method={method!r} not one of {_VALID_VIDEO_METHODS}"
        )
    return method


def _api_guard(result):
    """Turn a silent API failure into a loud, typed error (F1 fix).

    If a signed-API call returned NOTHING *because* the most recent fetch
    actually failed (signing down / IP bot-challenged / endpoint blocked),
    raise the matching typed exception instead of handing back an empty
    list/dict that looks identical to a genuine "0 results". A non-empty
    result, or an empty result with no recorded failure (a true zero), passes
    through untouched — so partial collections and real empties still work.

    We read-and-CONSUME ``last_failure`` (reset it to None) so a failure left
    over from this call — e.g. a partial multi-page collection whose final page
    failed — can't leak into the NEXT call and false-raise on a clean empty.
    """
    api = _get_pyk().api
    lf = getattr(api, "last_failure", None)
    api.last_failure = None  # consume — don't let it bleed into the next call
    if result:
        return result
    if lf == "signing":
        raise SigningFailed(
            "TikTok signing is unavailable (byted_acrawler did not load). "
            "This is usually a cold/headless/blocked session. Run pyk.healthcheck(); "
            "switch network (hotspot/VPN) or re-login, then retry."
        )
    if lf == "challenged" or lf == "decode" or (lf or "").startswith("status:"):
        raise Throttled(
            f"TikTok returned no data ({lf}) — likely bot-challenge or IP block. "
            "Run pyk.healthcheck(); switch network or wait a while, then retry."
        )
    if lf == "error":
        raise PyktokError("Network error talking to TikTok (see logs with set_verbosity('info')).")
    return result  # last_failure is None -> genuine empty result


# Which engine produced the most recent mode='auto' result. Read-only convenience
# (e.g. pyk.last_engine) — the authoritative per-row record is the CSV's
# `data_collection_engine` column.
last_engine: Optional[str] = None


def _auto(fn_name, api_call, tikwm_call, dom_call=None):
    """mode='auto': exhaust the engines in order — signed API → tikwm → DOM.

    Each attempt AND the engine that finally succeeds are logged at WARNING so
    the user can see which engine produced the data (Deen 1.c/3.d). The API
    result is guarded so a silent empty still triggers the fallback rather than
    returning []. ``dom_call`` is the optional third tier; pass it only where a
    DOM data-path exists (e.g. comments); with no ``dom_call`` a tikwm failure
    propagates.

    A ``RuntimeError`` from the API tier (e.g. no browser session) is treated as
    a tier failure and falls through to tikwm, which needs no session — so
    ``mode='auto'`` works without ``specify_browser`` instead of crashing.

    A genuine empty result (the entity really has 0 items) is NOT a failure and
    is returned as-is — only real engine failures fall through.
    """
    global last_engine
    # tier 1: signed hidden API
    try:
        result = _api_guard(api_call())
        last_engine = "api"
        logger.warning("%s: collected via api (mode='auto')", fn_name)
        return result
    except (SigningFailed, Throttled, PyktokError, RuntimeError) as exc:
        logger.warning("%s: api failed (%s) — trying tikwm next (mode='auto')",
                       fn_name, type(exc).__name__)
    # tier 2: tikwm public mirror
    try:
        result = tikwm_call()
        last_engine = "tikwm"
        logger.warning("%s: collected via tikwm (mode='auto')", fn_name)
        return result
    except Exception as exc:
        if dom_call is None:
            logger.warning("%s: tikwm failed (%s); no DOM fallback for this call — raising (mode='auto')",
                           fn_name, type(exc).__name__)
            raise
        logger.warning("%s: tikwm failed (%s) — trying DOM next (mode='auto')",
                       fn_name, type(exc).__name__)
    # tier 3: DOM page-scrape
    result = dom_call()
    last_engine = "dom"
    logger.warning("%s: collected via dom (mode='auto')", fn_name)
    return result


def _dom_comments_as_records(video_url, count):
    """DOM comment scrape returned as a list[dict] (matching the api/tikwm
    getter shape) so it can serve as the DOM tier of get_video_comments(auto).
    Runs the DOM engine inline on the current dispatcher worker (no re-entry)."""
    df = _get_pyk().dom.save_tiktok_comments_dom(
        video_url, comment_count=count, save_comments=False, return_comments=True,
    )
    if df is None or getattr(df, "empty", True):
        return []
    return df.to_dict("records")


_TIKWM = None  # type: ignore


def _get_tikwm():
    """Lazy singleton — keeps requests.Session warm across calls."""
    global _TIKWM
    if _TIKWM is None:
        from .tikwm import TikwmEngine
        _TIKWM = TikwmEngine()
    return _TIKWM


class _Pyk:
    """Holds the shared BrowserSession + DOMEngine + APIEngine."""

    def __init__(self, session: BrowserSession):
        self.session = session
        self.dom = DOMEngine(session)
        self.api = APIEngine(session)

    def close(self) -> None:
        try:
            self.session.close()
        except Exception:
            pass


_pyk: Optional[_Pyk] = None


def _get_pyk() -> _Pyk:
    if _pyk is None:
        raise RuntimeError(
            "This engine (signed API / DOM scrape) needs a browser session — call "
            "pyk.specify_browser(...) first. Or use mode='tikwm' / mode='auto', which need "
            "no browser and no login."
        )
    return _pyk


# ---- setup / teardown ----------------------------------------------------
_ENGINE_BY_BROWSER = {
    "chrome": "chromium", "chromium": "chromium", "edge": "chromium", "brave": "chromium",
    "firefox": "firefox", "safari": "webkit", "webkit": "webkit",
}


@jupyter_safe
def specify_browser(
    browser: str = "chrome",
    *,
    headless: bool = True,
    login_if_needed: bool = False,
    data_dir: Optional[str] = None,
    engine: Optional[str] = None,
) -> None:
    """Initialise the module singleton.

    Parameters
    ----------
    browser : 'chrome' | 'firefox' | 'edge' | 'safari' | '' / None
        Which browser to use. This sets BOTH the cookie-import source AND (when
        ``engine`` is not given) the Playwright engine to drive — so
        ``specify_browser('firefox')`` actually runs Firefox. Set to '' / None
        to skip the browser_cookie3 cookie path.
    headless : bool, default True
        Whether the persistent runtime browser runs headless.
    login_if_needed : bool, default False
        If True and no auth cookies are present, open a visible browser for
        one-time manual login. **Default False** so the common path never pops a
        login window (safe on CI / headless servers, and login is only needed for
        the signed API / restricted content). To log in, call ``pyk.login()`` or
        ``pyk.login_with_cookies(sessionid=...)`` explicitly. Existing saved
        cookies (``~/.pyktok_2026/state.json``) are still loaded regardless.
    data_dir : str | None
        Override the persistent profile location. Defaults to
        ``~/.pyktok_2026/browser_data`` for Chromium, and a per-engine dir
        (``browser_data_firefox`` / ``browser_data_webkit``) otherwise — since
        engines can't share a profile dir. Each engine logs in separately.
    engine : 'chromium' | 'webkit' | 'firefox' | None
        Which Playwright engine to drive. If None, inferred from ``browser``
        (chrome/edge -> chromium, firefox -> firefox, safari -> webkit).
        ``'webkit'`` is worth trying when TikTok has flagged a Chromium
        session — a different fingerprint, often lower detection. Requires the
        engine binary: ``playwright install <engine>``.
    """
    global _pyk
    if _pyk is not None:
        _pyk.close()
        _pyk = None

    # Infer the engine from the chosen browser unless the caller set it.
    if engine is None:
        engine = _ENGINE_BY_BROWSER.get((browser or "").lower(), "chromium")
    # Keep engines on separate profiles (they can't share a user-data-dir).
    if data_dir is None and engine != "chromium":
        import pathlib as _pl
        data_dir = str(_pl.Path.home() / ".pyktok_2026" / f"browser_data_{engine}")

    try:
        session = BrowserSession(
            data_dir=data_dir,
            headless=headless,
            login_if_needed=login_if_needed,
            cookie_browser=browser or None,
            engine=engine,
        ).launch()
    except SetupRequired:
        _setup()
        session = BrowserSession(
            data_dir=data_dir,
            headless=headless,
            login_if_needed=login_if_needed,
            cookie_browser=browser or None,
            engine=engine,
        ).launch()

    _pyk = _Pyk(session)


@jupyter_safe
def close() -> None:
    global _pyk, _TIKWM
    if _pyk is not None:
        _pyk.close()
        _pyk = None
    _TIKWM = None  # next call will rebuild the requests.Session
    # NOTE: We intentionally do NOT shut down the dispatcher worker thread
    # here. close() runs *inside* the worker (because of @jupyter_safe),
    # so joining itself would raise RuntimeError. Python's atexit will
    # clean the ThreadPoolExecutor up at interpreter shutdown.


def reset() -> None:
    """Recover from a hung call WITHOUT restarting the kernel.

    If a collection call got stuck (e.g. a flagged session dragged a fetch and
    you interrupted it with Ctrl-C), the single worker thread that owns the
    browser is still busy, so every later ``pyk.*`` call queues behind it and
    hangs. ``reset()`` abandons that worker and drops the browser session, so the
    NEXT ``specify_browser(...)`` starts clean. Any orphaned browser is cleared
    on the next launch.

    This is intentionally NOT routed through the worker (no ``@jupyter_safe``) —
    otherwise it would queue behind the very task it's trying to escape.

        pyk.reset()
        pyk.specify_browser('chrome')   # fresh worker + session
    """
    global _pyk, _TIKWM
    from ._dispatcher import reset_worker
    reset_worker()        # abandon the stuck worker; next call spawns a new one
    _pyk = None           # old session is owned by the abandoned thread — let it go
    _TIKWM = None
    logger.warning("pyk.reset(): worker abandoned and session cleared. "
                   "Call pyk.specify_browser(...) to start fresh.")


@jupyter_safe
def set_verbosity(level) -> None:
    _set_verbosity(level)


@jupyter_safe
def setup() -> None:
    _setup()


@jupyter_safe
def healthcheck(probe_user: str = "tiktok") -> Dict[str, Any]:
    """Check whether the signed-API path is usable RIGHT NOW.

    Signs one known request and reports the result so a flagged IP, a cold or
    headless session, or an expired login surfaces up front — instead of as
    silent empty CSVs mid-run. Returns
    ``{"ok": bool, "reason": str|None, "detail": str}``.

    Recommended at the start of any API-mode run::

        hc = pyk.healthcheck()
        if not hc["ok"]:
            raise SystemExit(f"API not usable: {hc['detail']}")
    """
    return _get_pyk().api.healthcheck(probe_user)


@jupyter_safe
def login() -> None:
    """Log in to TikTok: open the headed QR flow, and if that doesn't complete,
    automatically fall back to importing cookies from your system browser.

    The window opens on the homepage — click 'Log in' -> 'Use QR code', scan,
    confirm. If the QR stalls (usually a flagged IP), this then tries your
    system-browser TikTok cookies so login can still succeed without the QR.
    """
    pyk = _get_pyk()
    s = pyk.session
    s._interactive_login()   # writes state.json only if real auth cookies appear
    # Re-establish the runtime context so the new auth loads. This also runs the
    # state.json -> system-browser-cookie fallback chain — so a stalled QR
    # automatically falls back to your logged-in system browser. We disable the
    # interactive step here so it can't pop a second window.
    prev = s.login_if_needed
    s.login_if_needed = False
    try:
        s.launch()
    finally:
        s.login_if_needed = prev
    if s.has_auth():
        logger.warning("login OK — auth cookies loaded.")
    else:
        logger.warning(
            "login not completed: no auth cookies. The QR likely didn't finish "
            "(usually a flagged IP), and no system-browser TikTok cookies were found. "
            "Easiest fix: log into tiktok.com in your normal browser, copy the 'sessionid' "
            "cookie (DevTools → Application → Cookies), then "
            "pyk.login_with_cookies(sessionid='...'). Or switch network (hotspot/VPN) and "
            "retry pyk.login()."
        )


def _coerce_cookies(cookies):
    """Accept either {name: value} or a list of Playwright cookie dicts."""
    if isinstance(cookies, dict):
        base = {"domain": ".tiktok.com", "path": "/", "secure": True}
        return [{**base, "name": str(k), "value": str(v)} for k, v in cookies.items()]
    if isinstance(cookies, list):
        return cookies
    raise TypeError("cookies must be a dict {name: value} or a list of cookie dicts.")


@jupyter_safe
def login_with_cookies(sessionid: Optional[str] = None, *,
                       cookies=None, persist: bool = True) -> bool:
    """Log in by injecting cookies YOU copied from your own logged-in browser.

    No QR, no password, nothing leaves your machine. This is the most reliable
    path when the QR flow stalls or system-cookie import is blocked by Chrome's
    encryption.

    How to get your sessionid
    -------------------------
    1. Open tiktok.com in your normal browser and make sure you're logged in.
    2. DevTools (F12 / Cmd-Opt-I) → Application → Cookies → ``https://www.tiktok.com``.
    3. Copy the **Value** of the ``sessionid`` row.
    4. ``pyk.login_with_cookies(sessionid='paste-it-here')``

    Parameters
    ----------
    sessionid : str | None
        The ``sessionid`` cookie value (usually enough on its own).
    cookies : dict | list | None
        Alternatively, all your cookies as ``{name: value}`` or a list of
        Playwright cookie dicts (more complete = more robust, esp. for signing).
    persist : bool
        Save to ``state.json`` so future runs stay logged in (default True).

    Returns ``True`` if a live auth cookie is now present.
    """
    s = _get_pyk().session
    if cookies is not None:
        ok = s.set_cookies(_coerce_cookies(cookies), persist=persist)
    elif sessionid:
        ok = s.login_with_sessionid(sessionid, persist=persist)
    else:
        raise ValueError("Provide sessionid='...' or cookies={...}.")
    if ok:
        logger.warning("login_with_cookies OK — signed in as @%s.", s.auth_username or "?")
    else:
        logger.warning("login_with_cookies: cookies injected but no live auth detected. "
                       "Re-copy the 'sessionid' value from a logged-in tiktok.com tab.")
    return ok


@jupyter_safe
def login_with_state(path: str) -> bool:
    """Log in from a Playwright ``storage_state`` JSON file (cookies + origins).

    Useful if you exported a session elsewhere, or to restore a backed-up
    ``~/.pyktok_2026/state.json``.
    """
    return _get_pyk().session.login_with_state_file(path)


@jupyter_safe
def login_status(verify_signing: bool = False) -> Dict[str, Any]:
    """Report exactly where your session stands — so 'is it logged in?' is never
    a guess.

    Returns a dict with the engine, how auth was obtained (``auth_path``),
    whether a LIVE (non-expired) auth cookie is present, which auth cookies exist
    and their expiry, the resolved ``@username`` (if logged in), and a concrete
    ``next_step``. With ``verify_signing=True`` it also probes the signed-API
    path (one network call).
    """
    from ._browser import _AUTH_COOKIE_NAMES
    pyk = _get_pyk()
    s = pyk.session
    try:
        cookies = [c for c in s.context.cookies() if "tiktok" in c.get("domain", "")]
    except Exception:
        cookies = []
    now = time.time()
    present: Dict[str, str] = {}
    for c in cookies:
        if c.get("name") in _AUTH_COOKIE_NAMES:
            exp = c.get("expires", -1)
            if exp in (-1, None):
                present[c["name"]] = "session"
            elif exp < now:
                present[c["name"]] = "EXPIRED"
            else:
                present[c["name"]] = f"valid ~{int((exp - now) / 86400)}d"
    logged_in = s.has_auth()
    status: Dict[str, Any] = {
        "engine": s.engine,
        "cookie_source": s.cookie_browser,
        "auth_path": s.auth_path,
        "logged_in": logged_in,
        "auth_cookies": present,
        "username": (s.current_username() if logged_in else None),
        "signing_ok": None,
        "next_step": None,
    }
    if verify_signing:
        hc = pyk.api.healthcheck()
        status["signing_ok"] = hc["ok"]
        status["signing_detail"] = hc["detail"]
    if not logged_in:
        status["next_step"] = (
            "Not logged in. Log into tiktok.com in your browser, copy the 'sessionid' cookie "
            "(DevTools → Application → Cookies), then pyk.login_with_cookies(sessionid='...'). "
            "Note: mode='tikwm' and most mode='dom' calls work WITHOUT login."
        )
    elif verify_signing and not status["signing_ok"]:
        status["next_step"] = (
            "Logged in, but signing is unavailable (cold / headless / flagged session). "
            "Use mode='dom' or mode='tikwm', or re-init with specify_browser(headless=False)."
        )
    else:
        status["next_step"] = "Logged in." + ("" if not verify_signing else " Signing works.")
    return status


# ---- single video (always DOM) ------------------------------------------
@jupyter_safe
def get_tiktok_json(video_url: str, browser_name: Optional[str] = None):
    return _get_pyk().dom.get_tiktok_json(video_url)


def _save_tiktok_dom(content_url, save_video, metadata_fn, dir_path):
    """DOM path: read the page in the browser (needs a session)."""
    return _get_pyk().dom.save_tiktok(
        content_url, save_video=save_video, metadata_fn=metadata_fn, dir_path=dir_path,
    )


def _save_tiktok_tikwm(content_url, save_video, metadata_fn, dir_path):
    """tikwm path: no browser / no login. Same CSV schema via tikwm_to_itemstruct."""
    from .tikwm import tikwm_to_itemstruct
    tk = _get_tikwm()
    v = tk.get_video(content_url)
    if not v or not (v.get("id") or v.get("video_id")):
        return None
    row = generate_data_row(tikwm_to_itemstruct(v), engine="tikwm")
    combined = deduplicate(metadata_fn, row, key="video_id")
    combined.to_csv(metadata_fn, index=False)
    content_paths = []
    if save_video:
        p = tk.download_video(v, dir_path=dir_path)
        if p:
            content_paths.append(p)
    return content_paths, metadata_fn


@jupyter_safe
def save_tiktok(
    content_url: str,
    save_video: bool = False,
    metadata_fn: str = "",
    browser_name: Optional[str] = None,
    dir_path: Optional[str] = None,
    mode: str = "auto",
):
    """Save one video/photo's metadata to CSV (and its MP4/images if save_video).

    Parameters
    ----------
    mode : 'auto' | 'dom' | 'tikwm', default 'auto'
        'dom' reads the page in a browser (needs ``specify_browser`` + a session).
        'tikwm' uses the no-login public mirror — **works with no browser at all**.
        'auto' (default) tries DOM first (if a session exists) and falls back to
        tikwm, so ``pyk.save_tiktok(url)`` works whether or not you called
        ``specify_browser`` (fixes the old "call specify_browser first" error).

    Writes a metadata CSV by default — if you don't pass ``metadata_fn`` it is
    auto-named ``video_<id>_<timestamp>.csv`` in the current directory. Returns
    ``(content_paths, metadata_fn)``.
    """
    global last_engine
    if not metadata_fn:
        from .targets import extract_video_id as _evid
        metadata_fn = default_endpoint_filename("video", _evid(content_url) or "unknown")
    m = _resolve_mode(mode, True, {"dom", "tikwm", "auto"}, "save_tiktok")

    if m == "auto":
        result = None
        try:
            result = _save_tiktok_dom(content_url, save_video, metadata_fn, dir_path)
            if result:
                last_engine = "dom"
        except Exception as exc:
            logger.warning("save_tiktok: dom failed (%s) — trying tikwm (mode='auto')",
                           type(exc).__name__)
        if not result:
            result = _save_tiktok_tikwm(content_url, save_video, metadata_fn, dir_path)
            if result:
                last_engine = "tikwm"
    elif m == "tikwm":
        result = _save_tiktok_tikwm(content_url, save_video, metadata_fn, dir_path)
        last_engine = "tikwm"
    else:  # dom
        result = _save_tiktok_dom(content_url, save_video, metadata_fn, dir_path)
        last_engine = "dom"

    if not result:
        logger.warning("Nothing saved — couldn't read the post at %s (deleted/private, or a photo "
                       "post neither engine exposed). Try a different mode.", content_url)
        return result
    content_paths, mfn = result
    where = os.path.abspath(os.path.dirname(mfn) or ".")
    extra = f" + {len(content_paths)} media file(s)" if content_paths else ""
    logger.warning("Saved → %s%s  (in %s, via %s)",
                   os.path.basename(mfn) if mfn else "(metadata)", extra, where, last_engine)
    return result


@jupyter_safe
def save_tiktok_multi_urls(
    video_urls,
    save_video: bool = False,
    metadata_fn: str = "",
    sleep: int = 4,
    browser_name: Optional[str] = None,
    dir_path: Optional[str] = None,
    use_api: bool = True,
):
    """Save metadata for many videos — by DEFAULT one CSV per URL
    (``video_<id>_<timestamp>.csv``).

    Pass ``metadata_fn=...`` to instead combine every row into a single file.
    Returns the list of CSV paths written.
    """
    out = _get_pyk().dom.save_tiktok_multi_urls(
        video_urls, save_video=save_video, metadata_fn=metadata_fn,
        sleep=sleep, dir_path=dir_path,
    )
    files = out if isinstance(out, list) else ([out] if out else [])
    if files:
        where = os.path.abspath(os.path.dirname(files[0]) or ".")
        logger.warning("Saved → %d CSV file(s) in %s: %s",
                       len(files), where, ", ".join(os.path.basename(f) for f in files))
    return out


# ---- multi-page (user/hashtag/related/sound) ----------------------------
def _run_multi_page(tt_ent, ent_type, video_ct, save_video, metadata_fn,
                    dir_path, sleep, mode, video_method):
    """One engine's worth of multi-page collection.

    Undecorated on purpose: the auto-loop in ``save_tiktok_multi_page`` calls
    this once per engine, and re-entering the @jupyter_safe single-worker
    dispatcher (which is what calling the decorated function would do) deadlocks.
    The api tier raises (via ``_api_guard``) on a real signing/throttle failure
    so 'auto' can fall through; a genuine 0-item entity returns cleanly.
    """
    # Default video_method: tikwm if metadata mode is tikwm, else dom
    if save_video:
        vm = _resolve_video_method(video_method) if video_method else ("tikwm" if mode == "tikwm" else "dom")
    else:
        vm = "dom"  # unused

    # ---- mode == "dom" --------------------------------------------------
    if mode == "dom":
        pyk = _get_pyk()
        return pyk.dom.save_tiktok_multi_page_scroll(
            tt_ent, entity_type=ent_type, video_ct=video_ct,
            save_video=save_video, metadata_fn=metadata_fn, sleep=sleep,
        )

    # ---- mode == "tikwm" ------------------------------------------------
    if mode == "tikwm":
        from .tikwm import tikwm_to_itemstruct, TikwmRateLimit
        tk = _get_tikwm()
        if ent_type == "user":
            iterator = tk.iter_user_videos(tt_ent, count=video_ct)
        elif ent_type == "hashtag":
            iterator = tk.iter_hashtag_videos(tt_ent, count=video_ct)
        elif ent_type == "sound":
            iterator = tk.iter_sound_videos(tt_ent, count=video_ct)
        else:
            raise ValueError(f"tikwm does not support ent_type={ent_type!r}")

        all_rows: List[pd.DataFrame] = []
        for page in iterator:
            page_rows = [generate_data_row(tikwm_to_itemstruct(v), engine="tikwm") for v in page]
            all_rows.extend(page_rows)
            # Interleave MP4 download per page so play URLs don't expire
            if save_video and vm == "tikwm":
                for v in page:
                    tk.download_video(v, dir_path=dir_path)
            elif save_video and vm == "dom":
                # Build a TikTok URL and let DOM re-fetch per video
                pyk_local = _get_pyk()
                for v in page:
                    author = (v.get("author") or {}).get("unique_id", "")
                    vid    = v.get("video_id") or v.get("id", "")
                    if author and vid:
                        u = f"{TIKTOK_BASE}/@{author}/video/{vid}"
                        try:
                            pyk_local.dom.save_tiktok(u, save_video=True, metadata_fn="", dir_path=dir_path)
                        except Exception as exc:
                            logger.warning("DOM download failed for %s: %s", vid, exc)
                    time.sleep(random.uniform(0.3, max(0.3, sleep)))

        df = pd.concat(all_rows, ignore_index=True) if all_rows else pd.DataFrame()
        combined = deduplicate(metadata_fn, df, key="video_id")
        combined.to_csv(metadata_fn, index=False)
        return metadata_fn

    # ---- mode == "api" --------------------------------------------------
    pyk = _get_pyk()
    if ent_type == "user":
        videos = pyk.api.get_user_videos(tt_ent, video_ct)
    elif ent_type == "hashtag":
        videos = pyk.api.get_hashtag_videos(tt_ent, video_ct)
    elif ent_type == "sound":
        videos = pyk.api.get_sound_videos(tt_ent, video_ct)
    else:
        raise ValueError(f"mode='api' does not support ent_type={ent_type!r}")

    # Raise on a real signing/throttle failure (so 'auto' can fall through);
    # a genuine 0-item entity passes through as [].
    videos = _api_guard(videos)
    if not videos:
        logger.warning("hidden API returned 0 items for %s (%s)", tt_ent, ent_type)
        return metadata_fn

    rows = [generate_data_row(v, engine="api") for v in videos]
    df = pd.concat(rows, ignore_index=True) if rows else pd.DataFrame()
    combined = deduplicate(metadata_fn, df, key="video_id")
    combined.to_csv(metadata_fn, index=False)

    if save_video:
        for video_obj in videos:
            video_id = video_obj.get("id", "")
            author = safe(video_obj, "author", "uniqueId", default="")
            if not (video_id and author):
                continue
            video_url = f"{TIKTOK_BASE}/@{author}/video/{video_id}"
            if vm == "dom":
                try:
                    pyk.dom.save_tiktok(video_url, save_video=True, metadata_fn="", dir_path=dir_path)
                except Exception as exc:
                    logger.warning("DOM download failed for %s: %s", video_id, exc)
            elif vm == "api":
                # Download straight off the itemStruct the API already returned —
                # no per-video page re-navigation. Watermarked (TikTok's own CDN).
                # Runs on the dispatcher worker thread (this fn is @jupyter_safe),
                # so page.evaluate inside _download_file is greenlet-safe.
                # NOTE: these CDN URLs are time-signed (~minutes); fine for a
                # single page / modest counts, but for large multi-page runs the
                # earliest items may expire before we reach them. video_method='dom'
                # or 'tikwm' re-derive a fresh URL per item and avoid that.
                dl = (safe(video_obj, "video", "downloadAddr", default="")
                      or safe(video_obj, "video", "playAddr", default=""))
                if dl:
                    try:
                        from ._csv import media_filename
                        pyk.dom._download_file(dl, media_filename(author, video_id), dir_path)
                    except Exception as exc:
                        logger.warning("API download failed for %s: %s", video_id, exc)
                else:
                    logger.warning("no downloadAddr/playAddr on item %s", video_id)
            else:  # vm == "tikwm"
                tk = _get_tikwm()
                tk.download_by_tiktok_url(video_url, dir_path=dir_path)
            time.sleep(random.uniform(0.3, max(0.3, sleep)))

    return metadata_fn


@jupyter_safe
def save_tiktok_multi_page(
    tt_ent: str,
    ent_type: str = "user",
    video_ct: int = 30,
    headless: bool = True,
    save_video: bool = False,
    metadata_fn: str = "",
    dir_path: Optional[str] = None,
    sleep: int = 4,
    browser_name: Optional[str] = None,
    use_api: bool = True,
    mode: Optional[str] = None,
    video_method: Optional[str] = None,
):
    """Collect metadata for a user / hashtag / sound / video-related list.

    Parameters
    ----------
    tt_ent : str
        Username, hashtag, sound_id, or a video URL (for video_related).
    ent_type : 'user' | 'hashtag' | 'sound' | 'video_related'
    video_ct : int
        Target item count.
    save_video : bool
        If True, also download the MP4 for each item.
    metadata_fn : str
        Output CSV path. If empty, a default is generated.
    dir_path : str | None
        Directory for MP4 files when save_video=True. Defaults to CWD.
    mode : 'api' | 'dom' | 'tikwm' | None
        Which engine fetches the metadata list. If None, derived from
        legacy use_api flag for back-compat (True -> 'api', False -> 'dom').
    video_method : 'dom' | 'tikwm' | None
        Which engine downloads MP4s when save_video=True. Defaults to 'dom'
        for mode in {api, dom}, and to 'tikwm' for mode='tikwm' (interleaved,
        avoids URL expiry). Pass 'tikwm' with mode='api' to get
        watermark-free MP4s while still using signed metadata.
    """
    tt_ent = (tt_ent or "").strip()
    if ent_type == "user":
        tt_ent = normalize_username(tt_ent)
    elif ent_type == "hashtag":
        tt_ent = normalize_hashtag(tt_ent)

    if not metadata_fn:
        metadata_fn = default_multi_page_metadata_filename(tt_ent, ent_type)

    supported = {"user": {"api", "dom", "tikwm", "auto"},
                 "hashtag": {"api", "dom", "tikwm", "auto"},
                 "sound": {"api", "tikwm", "auto"},
                 "video_related": {"api", "dom", "auto"}}.get(ent_type)
    if supported is None:
        raise ValueError('ent_type must be "user", "hashtag", "sound", or "video_related"')

    # video_related: /api/related/item_list/ is unreliable; force DOM
    if ent_type == "video_related" and (mode == "api" or (mode is None and use_api)):
        logger.warning(
            "ent_type='video_related' with mode='api' is unreliable "
            "(TikTok throttles /api/related/item_list/). Using DOM mode instead."
        )
        if mode == "api":
            mode = "dom"
        use_api = False

    mode = _resolve_mode(mode, use_api, supported, fn_name="save_tiktok_multi_page")

    if mode == "auto":
        # Exhaust the engines this ent_type can serve, in order api → tikwm → DOM.
        # Skip the known-unreliable api path for video_related (it gets DOM).
        from .tikwm import TikwmRateLimit
        order = [m for m in ("api", "tikwm", "dom")
                 if m in supported and not (m == "api" and ent_type == "video_related")]
        global last_engine
        last = None
        for i, _m in enumerate(order):
            nxt = order[i + 1] if i + 1 < len(order) else None
            try:
                out = _run_multi_page(tt_ent, ent_type, video_ct, save_video,
                                      metadata_fn, dir_path, sleep, _m, video_method)
                last_engine = _m
                logger.warning("save_tiktok_multi_page: collected via %s (mode='auto')", _m)
                return out
            except (SigningFailed, Throttled, PyktokError, TikwmRateLimit, RuntimeError) as exc:
                last = exc
                logger.warning("save_tiktok_multi_page: %s failed (%s)%s (mode='auto')",
                               _m, type(exc).__name__,
                               f" — trying {nxt} next" if nxt else " — no engines left")
        if last:
            raise last
        return metadata_fn

    return _run_multi_page(tt_ent, ent_type, video_ct, save_video,
                           metadata_fn, dir_path, sleep, mode, video_method)


# ---- comments ------------------------------------------------------------
def _run_save_comments(video_url, filename, comment_count, save_comments,
                       return_comments, mode, timeout):
    """One engine's worth of comment work.

    Undecorated so save_tiktok_comments' auto-loop can call it once per engine
    without re-entering the @jupyter_safe single-worker dispatcher (which would
    deadlock). The api tier raises (via ``_api_guard``) on a real
    signing/throttle failure so 'auto' falls through; a genuine 0-comment video
    returns cleanly.
    """
    from .targets import extract_video_id

    # ---- tikwm ----------------------------------------------------------
    if mode == "tikwm":
        from .tikwm import TikwmRateLimit
        tk = _get_tikwm()
        comments = tk.get_video_comments(video_url, count=comment_count)
        vid = extract_video_id(video_url) or video_url
        # Always write whatever was collected before checking rate-limit state,
        # so partial pages are never lost.
        if save_comments and filename and comments:
            rows = [comment_export_row(c, vid, engine="tikwm") for c in comments]
            df = pd.DataFrame(rows, columns=COMMENT_EXPORT_COLUMNS)
            df = deduplicate(filename, df, key="cid")
            df.to_csv(filename, index=False)
            logger.info("tikwm: saved %d comments → %s", len(df), filename)
        if tk.last_call_rate_limited:
            # Re-raise so the outer loop stops cleanly; the partial CSV is on disk.
            raise TikwmRateLimit(
                f"tikwm rate-limited after {len(comments)} comments on {vid}"
            )
        if return_comments:
            return pd.DataFrame([
                {
                    "cid":            c.get("cid", c.get("id", "")),
                    "text":           c.get("text", ""),
                    "digg_count":     c.get("digg_count", 0),
                    "create_time":    c.get("create_time", 0),
                    "user_unique_id": (c.get("user") or {}).get("unique_id", ""),
                    "user_nickname":  (c.get("user") or {}).get("nickname", ""),
                }
                for c in comments
            ])
        return None

    # ---- dom ------------------------------------------------------------
    if mode == "dom":
        pyk = _get_pyk()
        return pyk.dom.save_tiktok_comments_dom(
            video_url, filename=filename, comment_count=comment_count,
            save_comments=save_comments, return_comments=return_comments,
        )

    # ---- api ------------------------------------------------------------
    pyk = _get_pyk()
    vid = extract_video_id(video_url) or str(video_url)
    # Fetch once, guarded: a real signing/throttle failure raises (so 'auto'
    # can fall through); a genuine 0-comment video passes through as [].
    comments = _api_guard(pyk.api.get_video_comments(video_url, comment_count, timeout=timeout))
    if save_comments and filename:
        if comments:
            rows = [comment_export_row(c, vid, engine="api") for c in comments]
            df = pd.DataFrame(rows, columns=COMMENT_EXPORT_COLUMNS)
            df = deduplicate(filename, df, key="cid")
            df.to_csv(filename, index=False)
            logger.info("Saved %d comments → %s", len(df), filename)
        else:
            pd.DataFrame(columns=COMMENT_EXPORT_COLUMNS).to_csv(filename, index=False)
            logger.warning("No comments found for %s — wrote header-only CSV → %s", vid, filename)
    if return_comments:
        rows = []
        for c in comments:
            user = c.get("user", {}) if isinstance(c.get("user"), dict) else {}
            rows.append({
                "cid":            c.get("cid", ""),
                "text":           c.get("text", ""),
                "digg_count":     c.get("digg_count", 0),
                "create_time":    c.get("create_time", 0),
                "user_unique_id": user.get("unique_id", ""),
                "user_nickname":  user.get("nickname", ""),
            })
        return pd.DataFrame(rows)
    return None


@jupyter_safe
def save_tiktok_comments(
    video_url: str,
    filename: str = "",
    comment_count: int = 30,
    headless: bool = True,
    save_comments: bool = True,
    return_comments: bool = True,
    use_api: bool = True,
    use_tikwm: bool = False,
    mode: Optional[str] = None,
    timeout: Optional[float] = None,
):
    """Save / return comments for a single video.

    Parameters
    ----------
    mode : 'api' | 'dom' | 'tikwm' | 'auto' | None
        Engine used. If None, derived from legacy `use_api`/`use_tikwm`
        flags. mode= wins when set. 'auto' exhausts all three in order
        (api → tikwm → DOM), so a flagged signed session still gets comments.
    timeout : float | None
        Seconds budget for the whole call (api mode). Pagination stops and
        saves what it has once spent — so one viral/stuck video can't hang a
        batch run. None = no limit.
    """
    # Legacy flag → mode (use_tikwm wins over use_api per prior behaviour)
    if mode is None:
        if use_tikwm:
            mode = "tikwm"
        else:
            mode = "api" if use_api else "dom"
    mode = _resolve_mode(mode, use_api, {"api", "dom", "tikwm", "auto"},
                         fn_name="save_tiktok_comments")

    # Auto-name (Scheme C) once, so every engine writes the same
    # comments_<vid>_<timestamp>.csv when no filename is given.
    if save_comments and not filename:
        from .targets import extract_video_id as _evid
        filename = default_endpoint_filename("video_comments", _evid(video_url) or str(video_url))

    if mode == "auto":
        # Exhaust the engines in order api → tikwm → DOM; a flagged signed
        # session still yields comments off the page.
        from .tikwm import TikwmRateLimit
        global last_engine
        order = ("api", "tikwm", "dom")
        last = None
        for i, _m in enumerate(order):
            nxt = order[i + 1] if i + 1 < len(order) else None
            try:
                out = _run_save_comments(video_url, filename, comment_count,
                                         save_comments, return_comments, _m, timeout)
                last_engine = _m
                logger.warning("save_tiktok_comments: collected via %s (mode='auto')", _m)
                return out
            except (SigningFailed, Throttled, PyktokError, TikwmRateLimit, RuntimeError) as exc:
                last = exc
                logger.warning("save_tiktok_comments: %s failed (%s)%s (mode='auto')",
                               _m, type(exc).__name__,
                               f" — trying {nxt} next" if nxt else " — no engines left")
        if last:
            raise last
        return None

    return _run_save_comments(video_url, filename, comment_count,
                              save_comments, return_comments, mode, timeout)


@jupyter_safe
def get_tiktok_comments(video_url: str, comment_count: int = 30, use_api: bool = True):
    return save_tiktok_comments(
        video_url, comment_count=comment_count, use_api=use_api,
        save_comments=False, return_comments=True,
    )


# ---- hidden-API convenience functions ------------------------------------
@jupyter_safe
def get_user_info(username: str, mode: Optional[str] = None,
                  use_api: bool = True) -> Optional[Dict]:
    m = _resolve_mode(mode, use_api, {"api", "tikwm", "auto"}, "get_user_info")
    if m == "auto":
        return _auto("get_user_info", lambda: _get_pyk().api.get_user_info(username),
                     lambda: _get_tikwm().get_user_info(username))
    if m == "tikwm":
        return _get_tikwm().get_user_info(username)
    return _api_guard(_get_pyk().api.get_user_info(username))


@jupyter_safe
def get_user_videos(username: str, count: int = 30,
                    mode: Optional[str] = None,
                    use_api: bool = True,
                    timeout: Optional[float] = None) -> List[Dict]:
    m = _resolve_mode(mode, use_api, {"api", "tikwm", "auto"}, "get_user_videos")
    if m == "auto":
        return _auto("get_user_videos",
                     lambda: _get_pyk().api.get_user_videos(username, count, timeout=timeout),
                     lambda: _get_tikwm().get_user_videos(username, count=count))
    if m == "tikwm":
        return _get_tikwm().get_user_videos(username, count=count)
    return _api_guard(_get_pyk().api.get_user_videos(username, count, timeout=timeout))


@jupyter_safe
def get_hashtag_videos(hashtag: str, count: int = 30,
                       mode: Optional[str] = None,
                       use_api: bool = True,
                       timeout: Optional[float] = None) -> List[Dict]:
    m = _resolve_mode(mode, use_api, {"api", "tikwm", "auto"}, "get_hashtag_videos")
    if m == "auto":
        return _auto("get_hashtag_videos",
                     lambda: _get_pyk().api.get_hashtag_videos(hashtag, count, timeout=timeout),
                     lambda: _get_tikwm().get_hashtag_videos(hashtag, count=count))
    if m == "tikwm":
        return _get_tikwm().get_hashtag_videos(hashtag, count=count)
    return _api_guard(_get_pyk().api.get_hashtag_videos(hashtag, count, timeout=timeout))


@jupyter_safe
def get_video_comments(video_url: str, count: int = 30,
                       mode: Optional[str] = None,
                       use_api: bool = True,
                       timeout: Optional[float] = None) -> List[Dict]:
    m = _resolve_mode(mode, use_api, {"api", "tikwm", "auto"}, "get_video_comments")
    if m == "auto":
        # api → tikwm → DOM: comments are the one getter with a real DOM data path.
        return _auto("get_video_comments",
                     lambda: _get_pyk().api.get_video_comments(video_url, count, timeout=timeout),
                     lambda: _get_tikwm().get_video_comments(video_url, count=count),
                     dom_call=lambda: _dom_comments_as_records(video_url, count))
    if m == "tikwm":
        return _get_tikwm().get_video_comments(video_url, count=count)
    return _api_guard(_get_pyk().api.get_video_comments(video_url, count, timeout=timeout))


@jupyter_safe
def get_related_videos(video_url: str, count: int = 16) -> List[Dict]:
    """Related-video list. tikwm has no related endpoint, so api/dom only."""
    return _api_guard(_get_pyk().api.get_related_videos(video_url, count))


@jupyter_safe
def get_trending_videos(count: int = 30, region: str = "US",
                        mode: Optional[str] = None,
                        use_api: bool = True) -> List[Dict]:
    m = _resolve_mode(mode, use_api, {"api", "tikwm", "auto"}, "get_trending_videos")
    if m == "auto":
        return _auto("get_trending_videos",
                     lambda: _get_pyk().api.get_trending_videos(count),
                     lambda: _get_tikwm().get_trending_videos(count=count, region=region))
    if m == "tikwm":
        return _get_tikwm().get_trending_videos(count=count, region=region)
    return _api_guard(_get_pyk().api.get_trending_videos(count))


@jupyter_safe
def search_users(keyword: str, count: int = 10) -> List[Dict]:
    """Search TikTok for USERS matching a keyword (API only).

    This is the clearly-named entry point. ``search_keyword`` is kept as a
    backward-compatible alias. For VIDEOS use ``search_videos``; for hashtags
    use ``search_hashtags``.
    """
    return _api_guard(_get_pyk().api.search_keyword(keyword, count))


@jupyter_safe
def search_keyword(keyword: str, count: int = 10) -> List[Dict]:
    """Deprecated alias for ``search_users`` (returns USERS, not videos).
    Kept for back-compat; prefer ``search_users``."""
    return _api_guard(_get_pyk().api.search_keyword(keyword, count))


@jupyter_safe
def search_videos(keyword: str, count: int = 10,
                  mode: Optional[str] = None,
                  use_api: bool = True,
                  timeout: Optional[float] = None) -> List[Dict]:
    """Keyword search returning VIDEOS (distinct from search_keyword)."""
    m = _resolve_mode(mode, use_api, {"api", "tikwm", "auto"}, "search_videos")
    if m == "auto":
        return _auto("search_videos",
                     lambda: _get_pyk().api.search_videos(keyword, count, timeout=timeout),
                     lambda: _get_tikwm().search_videos(keyword, count=count))
    if m == "tikwm":
        return _get_tikwm().search_videos(keyword, count=count)
    return _api_guard(_get_pyk().api.search_videos(keyword, count, timeout=timeout))


@jupyter_safe
def search_hashtags(keyword: str, count: int = 10) -> List[Dict]:
    """Hashtag discovery (returns challenge metadata, not videos). tikwm only."""
    return _get_tikwm().search_hashtags(keyword, count=count)


@jupyter_safe
def get_hashtag_info(hashtag: str, mode: Optional[str] = None,
                     use_api: bool = True) -> Optional[Dict]:
    """Hashtag/challenge detail (id, video/view counts). 'api' | 'tikwm' | 'auto'."""
    m = _resolve_mode(mode, use_api, {"api", "tikwm", "auto"}, "get_hashtag_info")
    if m == "auto":
        return _auto("get_hashtag_info", lambda: _get_pyk().api.get_hashtag_info(hashtag),
                     lambda: _get_tikwm().get_hashtag_info(hashtag))
    if m == "tikwm":
        return _get_tikwm().get_hashtag_info(hashtag)
    return _api_guard(_get_pyk().api.get_hashtag_info(hashtag))


@jupyter_safe
def get_sound_info(sound_id: str, mode: Optional[str] = None,
                   use_api: bool = True) -> Optional[Dict]:
    m = _resolve_mode(mode, use_api, {"api", "tikwm", "auto"}, "get_sound_info")
    if m == "auto":
        return _auto("get_sound_info", lambda: _get_pyk().api.get_sound_info(sound_id),
                     lambda: _get_tikwm().get_sound_info(sound_id))
    if m == "tikwm":
        return _get_tikwm().get_sound_info(sound_id)
    return _api_guard(_get_pyk().api.get_sound_info(sound_id))


@jupyter_safe
def get_sound_videos(sound_id: str, count: int = 30,
                     mode: Optional[str] = None,
                     use_api: bool = True,
                     timeout: Optional[float] = None) -> List[Dict]:
    m = _resolve_mode(mode, use_api, {"api", "tikwm", "auto"}, "get_sound_videos")
    if m == "auto":
        return _auto("get_sound_videos",
                     lambda: _get_pyk().api.get_sound_videos(sound_id, count, timeout=timeout),
                     lambda: _get_tikwm().get_sound_videos(sound_id, count=count))
    if m == "tikwm":
        return _get_tikwm().get_sound_videos(sound_id, count=count)
    return _api_guard(_get_pyk().api.get_sound_videos(sound_id, count, timeout=timeout))


@jupyter_safe
def get_comment_replies(comment_id: str, video_id: str, count: int = 50,
                        mode: Optional[str] = None, use_api: bool = True) -> List[Dict]:
    """Replies under a single comment. 'api' | 'tikwm' | 'auto'."""
    m = _resolve_mode(mode, use_api, {"api", "tikwm", "auto"}, "get_comment_replies")
    if m == "auto":
        return _auto("get_comment_replies",
                     lambda: _get_pyk().api.get_comment_replies(comment_id, video_id, count=count),
                     lambda: _get_tikwm().get_comment_replies(comment_id, video_id, count=count))
    if m == "tikwm":
        return _get_tikwm().get_comment_replies(comment_id, video_id, count=count)
    return _api_guard(_get_pyk().api.get_comment_replies(comment_id, video_id, count=count))


@jupyter_safe
def collect(kind: str, targets, out_dir: str = "pyktok_out", count: int = 100,
            mode: str = "tikwm", timeout: Optional[float] = None,
            skip_existing: bool = True) -> List[Dict]:
    """Resumable batch collection — the loop you'd otherwise hand-write.

    For each target, writes a **stable-named** CSV (``<kind>_<target>.csv``) in
    ``out_dir``, skips targets already done, and **stops cleanly on a rate
    limit** so simply re-running resumes where it left off.

    Parameters
    ----------
    kind : 'user' | 'hashtag' | 'sound' | 'comments'
    targets : str | list[str]   usernames / hashtags / sound ids / video URLs
    out_dir : str               folder for the CSVs (created if missing)
    count : int                 videos (or comments) per target
    mode : 'tikwm' | 'api' | 'dom' | 'auto'
    timeout : float | None      per-target seconds budget (comments especially)
    skip_existing : bool        skip targets whose CSV already exists (resume)

    Returns a list of per-target dicts: ``{target, status, rows, file}``.
    """
    from pathlib import Path
    from .tikwm import TikwmRateLimit
    if isinstance(targets, str):
        targets = [targets]
    if kind not in ("user", "hashtag", "sound", "comments"):
        raise ValueError("collect: kind must be 'user' | 'hashtag' | 'sound' | 'comments'")
    out = Path(out_dir)
    out.mkdir(parents=True, exist_ok=True)
    results: List[Dict] = []
    for t in targets:
        if kind == "comments":
            from .targets import extract_video_id
            key = extract_video_id(t) or "".join(c for c in str(t) if c.isalnum())
        else:
            key = "".join(c for c in str(t) if c.isalnum() or c in "-_") or "x"
        fn = out / f"{kind}_{key}.csv"
        if skip_existing and fn.exists() and fn.stat().st_size > 0:
            try:
                rows = len(pd.read_csv(fn))
            except Exception:
                rows = 0
            results.append({"target": t, "status": "skip", "rows": rows, "file": str(fn)})
            continue
        try:
            if kind == "comments":
                save_tiktok_comments(t, filename=str(fn), comment_count=count,
                                     save_comments=True, return_comments=False,
                                     mode=mode, timeout=timeout)
            else:
                save_tiktok_multi_page(t, ent_type=kind, video_ct=count,
                                       metadata_fn=str(fn), mode=mode)
            rows = len(pd.read_csv(fn)) if fn.exists() else 0
            results.append({"target": t, "status": "ok", "rows": rows, "file": str(fn)})
            logger.info("collect: %s %r -> %d rows", kind, t, rows)
        except TikwmRateLimit:
            results.append({"target": t, "status": "rate-limit", "rows": 0, "file": str(fn)})
            logger.warning("collect: tikwm rate limit at %r — stopping; rerun later to resume", t)
            break
        except (SigningFailed, Throttled) as exc:
            results.append({"target": t, "status": "blocked", "rows": 0, "error": str(exc)[:100]})
            logger.warning("collect: blocked at %r (%s) — stopping; switch network and retry",
                           t, type(exc).__name__)
            break
        except Exception as exc:
            results.append({"target": t, "status": "error", "rows": 0, "error": str(exc)[:120]})
            logger.warning("collect: error at %r: %s", t, exc)
    return results


@jupyter_safe
def download_video(video_url: str, dir_path: Optional[str] = None,
                   hd: bool = True, mode: Optional[str] = None,
                   use_api: bool = True) -> Optional[str]:
    """Download a single video's MP4. Returns the saved file path.

    mode='dom' (default for legacy use_api=True path too): re-fetches page
    JSON for fresh downloadAddr, downloads via Playwright session cookies.
    Result has TikTok watermark.

    mode='tikwm': single call to /api/?url=... for the watermark-free hdplay
    URL, then plain HTTPS download. Faster, no auth, no watermark.
    """
    m = _resolve_mode(mode, use_api, {"dom", "tikwm"}, "download_video")
    if m == "tikwm":
        return _get_tikwm().download_by_tiktok_url(video_url, dir_path=dir_path, hd=hd)
    pyk = _get_pyk()
    result = pyk.dom.save_tiktok(video_url, save_video=True, metadata_fn="", dir_path=dir_path)
    # dom.save_tiktok returns (content_paths, metadata_fn); surface the saved file.
    try:
        paths = result[0] if isinstance(result, tuple) else None
        return paths[0] if paths else None
    except Exception:
        return None


@jupyter_safe
def get_playlist_info(playlist_id: str) -> Optional[Dict]:
    return _api_guard(_get_pyk().api.get_playlist_info(playlist_id))


@jupyter_safe
def get_playlist_videos(playlist_id: str, count: int = 30) -> List[Dict]:
    return _api_guard(_get_pyk().api.get_playlist_videos(playlist_id, count))


# ---- user_backup (yt-dlp + DOM fallback) --------------------------------
@jupyter_safe
def save_user(
    usernames,
    count: Optional[int] = None,
    *,
    save_video: bool = False,
    metadata_fn: str = "",
    sleep: int = 2,
    dir_path: Optional[str] = None,
    cookies_from_browser=None,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
):
    """Discover + scrape user videos via yt-dlp, then save in pyktok format.

    Use this when ``get_user_videos`` (the hidden-API path) returns 0 because
    TikTok is suppressing ``/api/post/item_list/`` for the account. yt-dlp
    uses a different extraction route and often still finds videos; we then
    feed those URLs through the regular per-video DOM scraper so the output
    CSV matches what every other ``save_*`` function produces.

    Parameters
    ----------
    usernames : str | list[str] | tuple[str, ...]
        One TikTok handle or a collection of them. Multiple users each get
        their own CSV (one per handle).
    count : int | None
        Max videos per user. ``None`` means "everything yt-dlp returns".
        If ``count`` exceeds the user's reported videoCount, it's clamped
        to videoCount.
    save_video : bool, default False
        Also download each video's MP4 alongside the metadata CSV.
    metadata_fn : str
        Output filename. Only honored for the single-username form. For
        multi-user calls each handle gets ``save_user_videos_<u>.csv``.
    sleep : int, default 2
        Per-video sleep when scraping; passes through to DOM engine.
    dir_path : str | None
        Optional directory for downloaded media when ``save_video=True``.
    cookies_from_browser : str | tuple | None, default None
        Optional. When set, yt-dlp imports a logged-in session from the
        named browser. Use ``"chrome"`` (or another supported name) for
        the common case, or pass the full yt-dlp tuple
        ``(browser, profile, keyring, container)`` for fine control.
    date_start, date_end : str | None, default None
        Optional ``"YYYYMMDD"`` bounds (inclusive) for restricting which
        videos to discover by upload date. Either side may be set
        independently — pass just ``date_start`` for "from this date
        onward" or just ``date_end`` for "up to this date". With both
        unset, no date filter is applied.

    Returns
    -------
    dict | list[dict]
        A single result dict when ``usernames`` is a string, else a list
        of result dicts (one per username). Each dict contains
        ``{"username", "discovered", "expected_total", "csv", "rows"}``.
    """
    from .user_backup import save_user_videos_via_backup

    pyk = _get_pyk()
    single = isinstance(usernames, str)
    handles = [usernames] if single else list(usernames)

    results: List[Dict[str, Any]] = []
    for i, u in enumerate(handles):
        u = normalize_username(u)
        if not u:
            continue

        # Look up the user's reported videoCount so we can warn on shortfall.
        # If get_user_info itself fails, we just proceed with the caller's count.
        expected_total: Optional[int] = None
        try:
            info = pyk.api.get_user_info(u)
            vc = safe(info or {}, "userInfo", "stats", "videoCount", default=None)
            if vc is not None:
                expected_total = int(vc)
        except Exception as exc:
            logger.debug("get_user_info failed for @%s (continuing): %s", u, exc)

        effective_count = count
        if effective_count is None:
            effective_count = expected_total
        elif expected_total and effective_count > expected_total:
            logger.info(
                "@%s: clamping requested count=%d to videoCount=%d",
                u, effective_count, expected_total,
            )
            effective_count = expected_total

        # metadata_fn override only applies to the single-username form.
        mfn = metadata_fn if (single and metadata_fn) else ""

        result = save_user_videos_via_backup(
            pyk.session, pyk.dom, u, effective_count,
            save_video=save_video,
            metadata_fn=mfn,
            sleep=sleep,
            dir_path=dir_path,
            expected_total=expected_total,
            cookies_from_browser=cookies_from_browser,
            date_start=date_start,
            date_end=date_end,
        )
        logger.info(
            "save_user @%s: discovered=%d expected=%s rows=%s csv=%s",
            u, result["discovered"], result["expected_total"],
            result["rows"], result["csv"],
        )
        results.append(result)

    return results[0] if (single and results) else results
