"""Playwright wrapper with a persistent on-disk context.

One BrowserSession is shared by DOMEngine and APIEngine. The persistent context
lives at ~/.pyktok_2026/browser_data/ so cookies survive across runs. Three
paths to a logged-in session, in order: persistent context → browser_cookie3
import → interactive headed login.
"""
from __future__ import annotations

import os
import platform
import random
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from .exceptions import SetupRequired
from ._logging import get_logger
from .targets import TIKTOK_BASE

logger = get_logger("browser")

# Cookies that indicate a real logged-in TikTok session.
# NOTE: tt_chain_token, ttwid, msToken, _ttp all get set for logged-out
# visitors too, so they are NOT included here.
_AUTH_COOKIE_NAMES = {
    # Classic web-login cookies
    "sessionid", "sessionid_ss", "sid_tt", "sid_guard",
    "ssid_ucp_v1", "uid_tt", "passport_csrf_token",
    # QR / "TikTok Open" login flow (2025+ — same cookies, _tt_open suffix)
    "sessionid_tt_open", "sessionid_ss_tt_open",
    "sid_tt_tt_open", "sid_guard_tt_open",
    "sid_ucp_v1_tt_open", "ssid_ucp_v1_tt_open",
    "uid_tt_tt_open", "uid_tt_ss_tt_open",
}

_USER_AGENT = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
    "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
)

DEFAULT_DATA_DIR = Path.home() / ".pyktok_2026" / "browser_data"
DEFAULT_STATE_FILE = Path.home() / ".pyktok_2026" / "state.json"


def _no_display() -> bool:
    """True on a Linux box with no GUI display — a headed browser can't launch
    there (the headless-server case). macOS/Windows always have a window server."""
    if platform.system() != "Linux":
        return False
    return not (os.environ.get("DISPLAY") or os.environ.get("WAYLAND_DISPLAY"))


def _has_auth_cookies(cookies) -> bool:
    names = {c["name"] for c in cookies}
    return bool(names & _AUTH_COOKIE_NAMES)


def _live_auth_cookies(cookies) -> List[Dict[str, Any]]:
    """Auth cookies that are present AND not expired.

    Name-presence alone is a lie: an expired ``sessionid`` keeps its name but no
    longer authenticates, so a run proceeds and then fails silently. Playwright
    reports ``expires == -1`` for session cookies (no on-disk expiry) — those
    count as live. A positive ``expires`` in the past does not.
    """
    now = time.time()
    live = []
    for c in cookies:
        if c.get("name") not in _AUTH_COOKIE_NAMES:
            continue
        exp = c.get("expires", -1)
        if exp in (-1, None) or (isinstance(exp, (int, float)) and exp > now):
            live.append(c)
    return live


def _username_from_page_json(html) -> Optional[str]:
    """Best-effort: pull the signed-in @handle from a TikTok page's embedded JSON.

    The handle lives in different spots across page versions, so we try several.
    Returns None if none match — which does NOT prove logged-out (the page may
    have rendered without the app-context user).
    """
    from ._csv import extract_page_json, safe
    js = extract_page_json(html)
    if not js:
        return None
    candidates = [
        ("__DEFAULT_SCOPE__", "webapp.app-context", "user", "uniqueId"),
        ("__DEFAULT_SCOPE__", "webapp.app-context", "userInfo", "user", "uniqueId"),
        ("__DEFAULT_SCOPE__", "webapp.app-context", "user", "uniqueID"),
        ("AppContext", "appContext", "user", "uniqueId"),
        ("AppContext", "user", "uniqueId"),
    ]
    for path in candidates:
        u = safe(js, *path, default="")
        if u:
            return u
    return None


def _present_login_qr(page) -> bool:
    """Best-effort: get the QR login code on screen.

    TikTok greets logged-out visitors with an interest-picker / cookie modal
    ("What would you like to watch on TikTok?") that sits on top of the page, so
    we first dismiss that, then click 'Log in' → 'Use QR code', retrying because
    the modal renders late and the markup shifts. Returns True only if a QR image
    actually becomes visible. Never raises — on a miss the user clicks through
    manually (or, more reliably, uses pyk.login_with_cookies).
    """
    close_selectors = [
        "[data-e2e='modal-close-inner-button']",
        "div[role='dialog'] button[aria-label='Close']",
        "div[role='dialog'] [aria-label='Close']",
    ]
    login_selectors = [
        "[data-e2e='top-login-button']",
        "#header-login-button",
        "div[role='dialog'] button:has-text('Log in')",   # the interest modal's own Log in
        "button:has-text('Log in')",
        "a:has-text('Log in')",
    ]
    qr_tab_selectors = [
        "[data-e2e='qr-code']",
        "div:has-text('Use QR code')",
        "text=Use QR code",
        "text=QR code",
    ]

    def _try_click(selectors, timeout=3000) -> bool:
        for sel in selectors:
            try:
                loc = page.locator(sel).first
                if loc.count() and loc.is_visible():
                    loc.click(timeout=timeout)
                    return True
            except Exception:
                continue
        return False

    def _qr_visible() -> bool:
        for sel in ("[data-e2e='qr-code'] img", "[data-e2e='qr-code'] canvas",
                    "img[alt*='QR']", "canvas"):
            try:
                if page.locator(sel).first.is_visible():
                    return True
            except Exception:
                pass
        return False

    try:
        time.sleep(2.0)                 # let the interest/cookie modal render
        _try_click(close_selectors)     # dismiss it if present (best-effort)
        for _ in range(3):
            _try_click(login_selectors)
            time.sleep(1.5)
            _try_click(qr_tab_selectors)
            time.sleep(1.5)
            if _qr_visible():
                return True
        return _qr_visible()
    except Exception:
        return False


def save_storage_state(context, path: Path) -> None:
    """Dump context cookies + localStorage to a JSON file. Idempotent."""
    path.parent.mkdir(parents=True, exist_ok=True)
    context.storage_state(path=str(path))


class BrowserSession:
    """Playwright Chromium context with auth handling via storage_state.

    Auth flow (each is tried in order; first hit wins):
      1. ``state.json`` (Playwright storage_state) exists and has TikTok auth cookies
      2. ``browser_cookie3.<name>(domain_name='.tiktok.com')`` returns auth cookies
      3. Interactive headed login (if ``login_if_needed=True``)

    On every successful login we write ``state.json`` so future runs skip
    the interactive flow entirely.
    """

    def __init__(
        self,
        data_dir: Optional[os.PathLike] = None,
        state_file: Optional[os.PathLike] = None,
        headless: bool = True,
        login_if_needed: bool = False,
        login_timeout: int = 300,
        cookie_browser: Optional[str] = "chrome",
        engine: str = "chromium",
    ):
        self.data_dir = Path(data_dir) if data_dir else DEFAULT_DATA_DIR
        self.state_file = Path(state_file) if state_file else DEFAULT_STATE_FILE
        self.headless = headless
        self.login_if_needed = login_if_needed
        self.login_timeout = login_timeout
        self.cookie_browser = cookie_browser
        self.engine = engine if engine in ("chromium", "webkit", "firefox") else "chromium"

        self._pw = None
        self._context = None
        self._page = None
        self.auth_username: Optional[str] = None
        self.auth_path: str = "none"  # "state_file" / "browser_cookie3" / "interactive" / "none"

    # --- lifecycle -----------------------------------------------------
    def launch(self) -> "BrowserSession":
        self._start_runtime_context()
        self._establish_auth()
        return self

    def _clear_profile_lock(self) -> None:
        """Kill an ORPHANED browser still holding this profile dir + remove stale
        Singleton lock files, so a relaunch can take the profile. Called only on
        the launch retry path (after a genuine lock error) — NOT on the happy
        path — so it never touches a browser this session legitimately owns.
        """
        import os, signal, subprocess
        import time as _t
        ddir = str(self.data_dir)
        killed = False
        try:
            # Match the EXACT launch flag, not the bare path — only a browser
            # carries `--user-data-dir=<ddir>`, so this won't hit an editor or
            # backup job that merely mentions the path.
            out = subprocess.run(["pgrep", "-f", f"--user-data-dir={ddir}"],
                                 capture_output=True, text=True, timeout=5)
            me = os.getpid()
            for tok in out.stdout.split():
                try:
                    pid = int(tok)
                except ValueError:
                    continue
                if pid == me:
                    continue
                try:
                    os.kill(pid, signal.SIGTERM)
                    killed = True
                except (ProcessLookupError, PermissionError):
                    pass
        except Exception as exc:  # pgrep absent (e.g. Windows) or other — skip
            logger.debug("profile-lock process sweep skipped: %s", exc)
        if killed:
            _t.sleep(0.8)  # give them a moment to release the dir
            logger.info("closed an orphaned browser on %s before relaunch", ddir)
        for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
            try:
                (self.data_dir / name).unlink()
            except FileNotFoundError:
                pass
            except Exception:
                pass

    def _start_runtime_context(self) -> None:
        """Launch the headless Chromium for normal runtime use."""
        from playwright.sync_api import sync_playwright
        try:
            from playwright._impl._errors import Error as PWError  # type: ignore
        except Exception:
            PWError = Exception  # type: ignore

        self.data_dir.mkdir(parents=True, exist_ok=True)
        # Close our OWN prior context cleanly before relaunching, so we never
        # leak a context or open two persistent contexts on the same profile
        # dir (happens on the login() re-launch path). Orphans from a crashed
        # prior run are handled reactively in the launch retry below — we do NOT
        # sweep here, so we never SIGTERM a browser this session owns.
        if self._context is not None:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
            self._page = None
        if self._pw is None:
            self._pw = sync_playwright().start()

        # Chromium / Firefox accept Chromium-style command-line flags; WebKit
        # ignores them. We only pass them when applicable.
        # NOTE: --no-sandbox / --disable-dev-shm-usage are Docker/Linux-only
        # hardening flags. On macOS they trigger an "unsupported command-line
        # flag" banner that TikTok can use as a bot signal. Drop them on
        # non-Linux platforms.
        # Chromium shows a "You are using an unsupported command-line flag"
        # banner for flags on its hardcoded list — including
        # --disable-blink-features=AutomationControlled, --no-sandbox, etc.
        # TikTok's bot detection uses this banner's appearance as one signal.
        # Strip --enable-automation via ignore_default_args (below) and DON'T
        # add any of these flags here. On Linux we have to keep --no-sandbox
        # for container/CI environments, but on macOS / Windows we don't.
        import platform as _platform
        # --mute-audio: the signing warm-up loads the For You feed, which autoplays
        # videos; keep the runtime browser silent so the user isn't startled by
        # TikTok audio from a headless window.
        _chromium_args = ["--mute-audio"]
        if _platform.system() == "Linux":
            _chromium_args += ["--no-sandbox", "--disable-dev-shm-usage"]
        # Playwright's default args include `--enable-automation`, which
        # makes Chromium show the "Chrome is being controlled by automated
        # test software" banner AND sets navigator.webdriver=true. TikTok
        # uses both signals for bot detection. Strip them via
        # ignore_default_args.
        _ignore_defaults = [
            "--enable-automation",
            "--enable-blink-features=IdleDetection",
            "--no-sandbox",                 # strip if Playwright adds it
            "--disable-dev-shm-usage",      # strip if Playwright adds it
        ]
        launch_args = dict(
            user_data_dir=str(self.data_dir),
            headless=self.headless,
            user_agent=_USER_AGENT,
            viewport={"width": 1280, "height": 800},
            locale="en-US",
            timezone_id="America/New_York",
        )
        if self.engine == "webkit":
            engine_obj = self._pw.webkit
            engine_label = "WebKit"
        elif self.engine == "firefox":
            engine_obj = self._pw.firefox
            launch_args["args"] = [a for a in _chromium_args if a != "--mute-audio"]
            launch_args["firefox_user_prefs"] = {"media.volume_scale": "0.0"}  # mute (Firefox)
            engine_label = "Firefox"
        else:
            engine_obj = self._pw.chromium
            launch_args["args"] = _chromium_args
            launch_args["ignore_default_args"] = _ignore_defaults
            engine_label = "Chromium"

        try:
            self._context = engine_obj.launch_persistent_context(**launch_args)
        except Exception as exc:  # PWError + TargetClosedError + others
            msg = str(exc)
            if "Executable doesn't exist" in msg or "browserType.launch" in msg:
                self._pw.stop()
                self._pw = None
                raise SetupRequired(
                    f"Playwright's {engine_label} binary is missing. "
                    f"Run: playwright install {self.engine}"
                ) from exc
            # A locked/orphaned profile ("Opening in existing browser session" /
            # TargetClosedError): clear the lock and retry once before giving up.
            import time as _t
            logger.warning("browser launch failed (%s) — clearing profile lock and retrying once",
                           type(exc).__name__)
            self._clear_profile_lock()
            _t.sleep(1.0)
            self._context = engine_obj.launch_persistent_context(**launch_args)

        self._context.add_init_script(_STEALTH_INIT)

        # If state.json exists, seed our cookies from it (storage_state is the
        # only thing that reliably round-trips TikTok auth across runs).
        self._load_state_file_into_context()

        if self._context.pages:
            self._page = self._context.pages[0]
        else:
            self._page = self._context.new_page()
        self._page.set_default_navigation_timeout(45000)
        self._page.set_default_timeout(30000)

        try:
            from playwright_stealth import Stealth
            Stealth().apply_stealth_sync(self._page)
        except Exception as exc:
            logger.debug("playwright_stealth not applied: %s", exc)

    def _load_state_file_into_context(self) -> None:
        """If state.json exists, inject its cookies/localStorage into the live context."""
        if not self.state_file.exists():
            return
        try:
            import json as _json
            state = _json.loads(self.state_file.read_text())
            cookies = state.get("cookies") or []
            if cookies:
                self._context.add_cookies(cookies)
                logger.debug("Loaded %d cookies from %s", len(cookies), self.state_file)
        except Exception as exc:
            logger.warning("Could not load %s: %s", self.state_file, exc)

    def close(self) -> None:
        try:
            if self._context is not None:
                self._context.close()
        except Exception:
            pass
        self._context = None
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None
        self._page = None

    def __enter__(self):
        return self.launch()

    def __exit__(self, *_):
        self.close()

    # --- auth ----------------------------------------------------------
    def _establish_auth(self) -> None:
        # 1. state.json was loaded in _start_runtime_context — check it took
        if self.has_auth():
            u = self.current_username()
            self.auth_path = "state_file"
            self.auth_username = u
            if u:
                logger.warning("Resumed login as @%s (from %s)", u, self.state_file.name)
            else:
                logger.warning(
                    "Loaded auth cookies from %s, but couldn't read your @handle off the homepage. "
                    "That's usually harmless (the API still works), but it can mean the session "
                    "rendered logged-out (cold/flagged). Confirm with "
                    "pyk.login_status(verify_signing=True).",
                    self.state_file.name,
                )
            return

        # 2. browser_cookie3 fallback
        if self.cookie_browser:
            try:
                if self._seed_from_browser_cookie3(self.cookie_browser):
                    if self.has_auth():
                        u = self.current_username() or "?"
                        self.auth_path = "browser_cookie3"
                        self.auth_username = u
                        logger.warning("Imported login from system %s as @%s",
                                       self.cookie_browser, u)
                        # Persist for next run
                        try:
                            save_storage_state(self._context, self.state_file)
                        except Exception as exc:
                            logger.debug("Could not save state file: %s", exc)
                        return
            except Exception as exc:
                logger.warning(
                    "Importing cookies from system %s failed: %s. On recent Chrome (v127+) "
                    "this is usually App-Bound Encryption / a macOS Keychain prompt blocking "
                    "decryption. Fix: log into tiktok.com in your browser, copy the 'sessionid' "
                    "cookie, and call pyk.login_with_cookies(sessionid='...').",
                    self.cookie_browser, exc,
                )

        # 3. Interactive headed login (opt-in). Never let it crash specify_browser:
        # on a display-less server it raises SetupRequired — warn and continue with
        # a usable session (tikwm/api still work), rather than aborting setup.
        if self.login_if_needed:
            try:
                self._interactive_login()
            except SetupRequired as exc:
                logger.warning("Skipping interactive login: %s", exc)
            else:
                if self.has_auth():
                    u = self.current_username() or "?"
                    self.auth_path = "interactive"
                    self.auth_username = u
                    logger.warning("Interactive login completed as @%s; saved to %s",
                                   u, self.state_file)
                    return

        logger.warning(
            "No TikTok auth cookies found. Collection via mode='tikwm'/'auto' works with no login; "
            "for the signed API or restricted content, run pyk.login() or "
            "pyk.login_with_cookies(sessionid='...')."
        )

    def has_auth(self, live: bool = True) -> bool:
        """Whether the context holds TikTok auth cookies.

        ``live=True`` (default) ignores expired cookies, so a dead session is
        reported as logged-OUT instead of masquerading as logged-in. Pass
        ``live=False`` for the old name-only check (used right after a fresh
        login, where every cookie is by definition current).
        """
        try:
            cookies = self._context.cookies()
        except Exception:
            return False
        # Only count cookies on tiktok.com (or its subdomains)
        tt = [c for c in cookies if "tiktok" in c.get("domain", "")]
        return bool(_live_auth_cookies(tt)) if live else _has_auth_cookies(tt)

    # --- manual credential injection (no QR / no shared password) -------
    def set_cookies(self, cookies: List[Dict[str, Any]], persist: bool = True) -> bool:
        """Inject raw cookie dicts into the live context; persist to state.json.

        This is the no-QR login path: the user copies their OWN cookies from a
        browser where they're already logged in. Returns True iff the context
        now holds a live TikTok auth cookie.
        """
        if self._context is None:
            raise RuntimeError("No browser context — call specify_browser(...) first.")
        norm: List[Dict[str, Any]] = []
        for c in cookies:
            name = c.get("name")
            val = c.get("value")
            if not name or val in (None, ""):
                continue
            cc: Dict[str, Any] = {
                "name": str(name),
                "value": str(val),
                "domain": c.get("domain") or ".tiktok.com",
                "path": c.get("path") or "/",
            }
            if c.get("expires") not in (None, ""):
                try:
                    cc["expires"] = float(c["expires"])
                except (TypeError, ValueError):
                    pass
            if "secure" in c:
                cc["secure"] = bool(c["secure"])
            if "httpOnly" in c:
                cc["httpOnly"] = bool(c["httpOnly"])
            norm.append(cc)
        if not norm:
            logger.warning("set_cookies: nothing to inject (no name/value pairs found).")
            return False
        try:
            self._context.add_cookies(norm)
        except Exception as exc:
            # Some Playwright builds reject extra fields — retry with the minimum.
            logger.debug("set_cookies: full add_cookies failed (%s) — retrying minimal", exc)
            minimal = [{"name": c["name"], "value": c["value"],
                        "domain": c["domain"], "path": c["path"]} for c in norm]
            self._context.add_cookies(minimal)
        ok = self.has_auth()
        if ok:
            self.auth_path = "manual_cookies"
            try:
                self.auth_username = self.current_username()
            except Exception:
                self.auth_username = None
            if persist:
                try:
                    save_storage_state(self._context, self.state_file)
                    logger.warning("Injected cookies and saved auth state → %s", self.state_file)
                except Exception as exc:
                    logger.error("Could not persist state file: %s", exc)
        else:
            logger.warning(
                "Injected %d cookie(s) but no live TikTok auth cookie is present. "
                "Copy the 'sessionid' VALUE from a tiktok.com tab where you're logged in "
                "(DevTools → Application → Cookies → https://www.tiktok.com).",
                len(norm),
            )
        return ok

    def login_with_sessionid(self, sessionid: str, persist: bool = True) -> bool:
        """Convenience: log in from just the ``sessionid`` cookie value."""
        sessionid = (sessionid or "").strip().strip('"').strip("'")
        if not sessionid:
            raise ValueError("sessionid is empty.")
        base = {"domain": ".tiktok.com", "path": "/", "secure": True, "httpOnly": True}
        return self.set_cookies([{**base, "name": "sessionid", "value": sessionid}],
                                persist=persist)

    def login_with_state_file(self, path) -> bool:
        """Load a Playwright storage_state JSON (cookies) as the session."""
        import json as _json
        src = Path(path)
        if not src.exists():
            raise FileNotFoundError(f"storage_state file not found: {src}")
        try:
            state = _json.loads(src.read_text())
        except Exception as exc:
            raise ValueError(f"could not parse storage_state JSON {src}: {exc}") from exc
        cookies = state.get("cookies") if isinstance(state, dict) else None
        if not cookies:
            logger.warning("storage_state %s has no cookies.", src)
            return False
        return self.set_cookies(cookies, persist=True)

    def current_username(self) -> Optional[str]:
        """Return @username of the logged-in user, or None.

        TikTok puts the signed-in handle in a few different spots depending on
        page version, so we try several before giving up. None here doesn't prove
        you're logged out — the homepage may just have rendered without the
        app-context user (cold/headless); check ``has_auth()`` / ``healthcheck()``.
        """
        try:
            self.go(TIKTOK_BASE, wait=2.5)
            html = self._page.content()
        except Exception:
            return None
        return _username_from_page_json(html)

    def _seed_from_browser_cookie3(self, browser_name: str) -> bool:
        """Pull cookies for .tiktok.com from system Chrome (or other supported browser)
        into the persistent context. Returns True if any cookies were injected."""
        import browser_cookie3
        getter = getattr(browser_cookie3, browser_name, None)
        if getter is None:
            logger.warning("browser_cookie3 has no reader for %r — supported: chrome, chromium, "
                           "firefox, edge, brave, opera, safari (varies by OS).", browser_name)
            return False
        try:
            jar = getter(domain_name=".tiktok.com")
        except Exception as exc:
            logger.warning(
                "Could not read %s cookies: %s. Recent Chrome encrypts cookies "
                "(App-Bound Encryption / Keychain) and browser_cookie3 can't always decrypt them. "
                "Use pyk.login_with_cookies(sessionid='...') instead — copy 'sessionid' from "
                "DevTools → Application → Cookies on a logged-in tiktok.com tab.",
                browser_name, exc,
            )
            return False
        cookies_to_add: List[Dict[str, Any]] = []
        for c in jar:
            if not c.value:
                continue
            cookie: Dict[str, Any] = {
                "name": c.name,
                "value": c.value,
                "domain": c.domain or ".tiktok.com",
                "path": c.path or "/",
            }
            if c.expires:
                try:
                    cookie["expires"] = float(c.expires)
                except Exception:
                    pass
            cookie["secure"] = bool(getattr(c, "secure", False))
            cookies_to_add.append(cookie)
        if not cookies_to_add:
            logger.warning(
                "No .tiktok.com cookies found in system %s. Log into tiktok.com in that browser "
                "first, or use pyk.login_with_cookies(sessionid='...').", browser_name)
            return False
        if not (set(c["name"] for c in cookies_to_add) & _AUTH_COOKIE_NAMES):
            logger.warning(
                "Read %d %s cookie(s) for tiktok.com but none are auth cookies — you're likely "
                "logged OUT in that browser. Log in there, or use pyk.login_with_cookies(sessionid='...').",
                len(cookies_to_add), browser_name)
        try:
            self._context.add_cookies(cookies_to_add)
        except Exception as exc:
            logger.warning("add_cookies failed while importing from %s: %s", browser_name, exc)
            return False
        return True

    def _interactive_login(self) -> None:
        """Open a headed browser, wait for the user to log in, save state.json,
        then re-seed the runtime context.

        We use a SEPARATE non-persistent context (different data_dir under
        a temp suffix) so the user can complete OAuth without races with the
        headless runtime profile. After login we explicitly export
        ``storage_state`` to disk — that's the only reliable persistence path."""
        # A headed window can't open on a display-less box (e.g. the PICL server)
        # — that's the TargetClosedError / "no XServer" crash. Fail fast with an
        # actionable message instead (Deen 2.a).
        if _no_display():
            raise SetupRequired(
                "Can't open a login window — this looks like a headless server (no DISPLAY). "
                "Log in on a machine with a screen and copy ~/.pyktok_2026/state.json over, or use "
                "pyk.login_with_cookies(sessionid='...'). Collection via mode='tikwm'/'auto' needs no login."
            )
        _label = {"webkit": "WebKit", "firefox": "Firefox"}.get(self.engine, "Chromium")
        logger.warning(
            "Opening visible %s. If a 'What would you like to watch' pop-up appears, close it, "
            "then click 'Log in' (top-right) -> 'Use QR code' and scan with your phone. The window "
            "closes itself once you're signed in (within %ds).\n"
            "  If the QR never appears or won't complete (common on a flagged IP / Firefox), close "
            "the window and use the reliable no-QR path instead:\n"
            "    pyk.login_with_cookies(sessionid='...')   "
            "# copy 'sessionid' from a logged-in tiktok.com tab: DevTools -> Application -> Cookies",
            _label, self.login_timeout,
        )

        # Reuse the existing playwright handle (we can't start a second
        # sync_playwright in the same thread). Tear down the runtime context
        # so its user_data_dir lock is released — Playwright errors if two
        # Chromiums share the same dir.
        had_runtime = self._context is not None
        if had_runtime:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
            self._page = None

        if self._pw is None:
            from playwright.sync_api import sync_playwright
            self._pw = sync_playwright().start()

        import tempfile
        login_dir = Path(tempfile.mkdtemp(prefix="pyktok_login_"))
        # Use the same engine for login so cookies are written by the same
        # browser that the runtime context will later read them with.
        # Match the runtime context: no banner-triggering flags for the
        # headed login window either. Strip --enable-automation by default.
        if self.engine == "webkit":
            login_engine = self._pw.webkit
            login_kwargs = {}
        elif self.engine == "firefox":
            login_engine = self._pw.firefox
            login_kwargs = {}
        else:
            import platform as _platform
            login_engine = self._pw.chromium
            # Mirror the runtime context's flag handling EXACTLY: strip the
            # bot-signal flags (--no-sandbox triggers the "unsupported
            # command-line flag" banner TikTok reads as automation). Only keep
            # --no-sandbox on Linux/containers where Chromium needs it to launch.
            _login_args = []
            if _platform.system() == "Linux":
                _login_args += ["--no-sandbox", "--disable-dev-shm-usage"]
            login_kwargs = {
                "args": _login_args,
                "ignore_default_args": [
                    "--enable-automation",
                    "--enable-blink-features=IdleDetection",
                    "--no-sandbox",
                    "--disable-dev-shm-usage",
                ],
            }
        try:
            login_ctx = login_engine.launch_persistent_context(
                user_data_dir=str(login_dir),
                headless=False,
                user_agent=_USER_AGENT,
                viewport={"width": 1280, "height": 800},
                locale="en-US",
                timezone_id="America/New_York",
                **login_kwargs,
            )
            page = login_ctx.pages[0] if login_ctx.pages else login_ctx.new_page()
            try:
                # Land on the HOMEPAGE, not /login. The deep-linked /login/qrcode
                # page often serves a stale QR that never completes; the QR you
                # generate via the homepage's "Log in -> Use QR code" menu is
                # fresh and reliable. So we open the site and let the user click
                # through that menu.
                page.goto(TIKTOK_BASE, wait_until="domcontentloaded", timeout=60000)
            except Exception as exc:
                logger.debug("login page goto error (continuing): %s", exc)

            # Best-effort: open the login dialog and select the QR tab so a fresh,
            # scannable QR is shown immediately — instead of leaving the user on a
            # passive homepage that "never proceeds". If TikTok's markup has
            # drifted, this silently no-ops and the user clicks through manually
            # (Log in → Use QR code), which still works.
            if _present_login_qr(page):
                logger.warning("QR code is on screen — scan it with the TikTok app to sign in.")
            else:
                logger.warning(
                    "Couldn't auto-open the QR (TikTok's pop-up/markup or a flagged IP got in the way). "
                    "In the window: close any pop-up, click 'Log in' -> 'Use QR code', then scan. "
                    "If that won't work, close the window and run "
                    "pyk.login_with_cookies(sessionid='...') — the reliable no-QR path.")

            self._wait_for_auth(login_ctx)

            # Only persist if the login ACTUALLY produced auth cookies. A
            # stalled / abandoned QR (common on a flagged IP) otherwise saves an
            # empty storage_state and clobbers a previously-good state.json —
            # which silently logs the user out. Never overwrite good auth with
            # nothing.
            try:
                tt_cookies = [c for c in login_ctx.cookies() if "tiktok" in c.get("domain", "")]
            except Exception:
                tt_cookies = []
            if _has_auth_cookies(tt_cookies):
                try:
                    if self.state_file.exists():
                        import shutil
                        shutil.copyfile(self.state_file, self.state_file.with_suffix(".json.prev"))
                except Exception as exc:
                    logger.debug("could not back up prior state file: %s", exc)
                try:
                    save_storage_state(login_ctx, self.state_file)
                    logger.warning("Saved auth state to %s", self.state_file)
                except Exception as exc:
                    logger.error("Could not save state file: %s", exc)
            else:
                logger.warning(
                    "Login did not produce auth cookies — leaving existing %s untouched "
                    "(not overwriting good auth with an empty session). The QR likely didn't "
                    "complete, usually a flagged IP. Try a clean network (hotspot/VPN) and "
                    "pyk.login() again, or import cookies from your logged-in system browser.",
                    self.state_file,
                )

            try:
                login_ctx.close()
            except Exception:
                pass
        finally:
            try:
                import shutil
                shutil.rmtree(login_dir, ignore_errors=True)
            except Exception:
                pass

        # Fully tear down the Playwright instance after the login dance so
        # no orphan cleanup callbacks fire on greenlets we no longer hold.
        # Without this we get noisy "Cannot switch to a different thread"
        # callback errors after the second context teardown.
        try:
            if self._pw is not None:
                self._pw.stop()
        except Exception:
            pass
        self._pw = None

        # Re-launch the runtime context (fresh playwright) if we had one before
        if had_runtime:
            self._start_runtime_context()

    def _wait_for_auth(self, context) -> bool:
        deadline = time.time() + self.login_timeout
        last_beat = 0.0
        while time.time() < deadline:
            try:
                cookies = context.cookies()
                if _has_auth_cookies(cookies):
                    # Settle: give a few seconds for additional cookies / localStorage
                    # to land after the redirect.
                    time.sleep(3)
                    return True
            except Exception:
                pass
            # Heartbeat so the user knows we're still waiting (and how long is left).
            now = time.time()
            if now - last_beat >= 30:
                last_beat = now
                logger.warning("Waiting for you to finish login… %ds left. (Scan the QR with "
                               "the TikTok app, or close the window to cancel.)",
                               int(deadline - now))
            time.sleep(2)
        return False

    # --- navigation ----------------------------------------------------
    @property
    def page(self):
        return self._page

    @property
    def context(self):
        return self._context

    def go(self, url: str, wait: float = 2.0) -> None:
        try:
            self._page.goto(url, wait_until="domcontentloaded", timeout=45000)
        except Exception as exc:
            logger.debug("goto(%s) raised %s — continuing", url, type(exc).__name__)
        # Simple sleep is more reliable than networkidle for TikTok — many of
        # TikTok's pages keep analytics/beacon connections open indefinitely,
        # so networkidle would always burn the full timeout and slow us down.
        if wait:
            time.sleep(wait + random.uniform(0.0, 0.6))
        # Small human-like mouse jitter (per davidteather/TikTok-Api pattern).
        try:
            self._page.mouse.move(random.randint(10, 300), random.randint(10, 300))
        except Exception:
            pass

    def page_source(self) -> str:
        try:
            return self._page.content()
        except Exception:
            return ""

    def get_ms_token(self) -> Optional[str]:
        try:
            cookies = self._context.cookies("https://www.tiktok.com")
        except Exception:
            return None
        for c in cookies:
            if c["name"] == "msToken":
                return c["value"]
        return None

    # ---------------- JS evaluation helpers ----------------
    def eval_js(self, expr: str, *args):
        return self._page.evaluate(expr, *args) if args else self._page.evaluate(expr)


# ---------------------------------------------------------------------------
# Stealth init script applied to every new document in the context
# ---------------------------------------------------------------------------
_STEALTH_INIT = """
(function(){
    try { Object.defineProperty(Navigator.prototype, 'webdriver', { get: () => undefined }); } catch(e){}
    if(!window.chrome){ window.chrome = {}; }
    if(!window.chrome.runtime){
        window.chrome.runtime = { id: undefined, connect: null, sendMessage: null };
    }
    try {
        const nd = Object.getPrototypeOf(navigator);
        Object.defineProperty(nd,'languages',{get:()=>['en-US','en']});
        Object.defineProperty(nd,'platform',{get:()=>'MacIntel'});
        Object.defineProperty(nd,'vendor',{get:()=>'Google Inc.'});
    } catch(e){}
    try {
        const gp = WebGLRenderingContext.prototype.getParameter;
        WebGLRenderingContext.prototype.getParameter = function(p){
            if(p===37445) return 'Intel Inc.';
            if(p===37446) return 'Intel Iris OpenGL Engine';
            return gp.apply(this, arguments);
        };
    } catch(e){}
})();
"""
