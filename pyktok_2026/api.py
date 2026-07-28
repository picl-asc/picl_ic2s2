"""APIEngine — TikTok hidden-API client.

Signs requests inside a real Playwright page (so byted_acrawler.frontierSign is
TikTok's own JS) and runs fetch() from that page so cookies and CORS just work.
"""
from __future__ import annotations

import json
import random
import re
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlencode, quote

import pandas as pd

from ._browser import BrowserSession
from ._csv import (
    COMMENT_EXPORT_COLUMNS,
    HEADERS,
    comment_export_row,
    default_endpoint_filename,
    deduplicate,
    safe,
)
from ._logging import get_logger
from ._signing import fetch_in_page, frontier_sign, wait_for_acrawler
from .exceptions import SigningFailed, TargetNotFound, Throttled
from .targets import (
    TIKTOK_BASE, VIDEO_ID_REGEX,
    extract_playlist_id, extract_sound_id, extract_video_id,
    normalize_hashtag, normalize_username,
)

logger = get_logger("api")


HIDDEN_API_ENDPOINTS = {
    "user_detail":     "/api/user/detail/",
    "user_videos":     "/api/post/item_list/",
    "user_liked":      "/api/favorite/item_list/",
    "user_playlists":  "/api/user/playlist",
    "hashtag_detail":  "/api/challenge/detail/",
    "hashtag_videos":  "/api/challenge/item_list/",
    "video_comments":  "/api/comment/list/",
    "comment_replies": "/api/comment/list/reply/",
    "related_videos":  "/api/related/item_list/",
    "trending":        "/api/recommend/item_list/",
    "search_keyword":    "/api/search/user/full/",
    "search_general":  "/api/search/general/full/",
    "sound_detail":    "/api/music/detail/",
    "sound_videos":    "/api/music/item_list/",
    "playlist_detail": "/api/mix/detail/",
    "playlist_videos": "/api/mix/item_list/",
}


class APIEngine:
    """Hidden-API TikTok client. Browser-based signing, cookie-authed fetch."""

    def __init__(self, session: BrowserSession):
        self.session = session
        self._base_params: Optional[Dict[str, str]] = None
        self._last_ok: Dict[str, Any] = {}
        # Records WHY the most recent fetch_api failed, so callers can tell a
        # genuine empty result ("endpoint returned 0 items") apart from a
        # silent failure ("signing down" / "bot-challenged"). None = last call
        # succeeded or hasn't run. One of: "signing", "challenged",
        # "status:<code>", "decode", "error".
        self.last_failure: Optional[str] = None

    # ---- session params (cached) ---------------------------------------
    def _ensure_base_params(self) -> Dict[str, str]:
        if self._base_params is not None:
            return self._base_params

        page = self.session.page

        def _js(expr: str, default: str = "") -> str:
            try:
                val = page.evaluate(f"() => ({expr})")
                return str(val) if val is not None else default
            except Exception:
                return default

        user_agent    = _js("navigator.userAgent", HEADERS.get("User-Agent", ""))
        language      = _js("navigator.language || navigator.userLanguage", "en")
        platform      = _js("navigator.platform", "MacIntel")
        tz_name       = _js("Intl.DateTimeFormat().resolvedOptions().timeZone", "America/New_York")
        screen_height = _js("window.screen && window.screen.height", "1080")
        screen_width  = _js("window.screen && window.screen.width", "1920")

        self._base_params = {
            "aid":              "1988",
            "app_language":     language,
            "app_name":         "tiktok_web",
            "browser_language": language,
            "browser_name":     "Mozilla",
            "browser_online":   "true",
            "browser_platform": platform,
            "browser_version":  user_agent,
            "channel":          "tiktok_web",
            "cookie_enabled":   "true",
            "device_id":        str(random.randint(10**18, 10**19 - 1)),
            "device_platform":  "web_pc",
            "focus_state":      "true",
            "from_page":        "user",
            "history_len":      str(random.randint(1, 10)),
            "is_fullscreen":    "false",
            "is_page_visible":  "true",
            "language":         language,
            "os":               platform,
            "priority_region":  "",
            "referer":          "",
            "region":           "US",
            "screen_height":    screen_height,
            "screen_width":     screen_width,
            "tz_name":          tz_name,
            "webcast_language": language,
        }
        return self._base_params

    # ---- core fetch ----------------------------------------------------
    def fetch_api(self, endpoint_key: str, params: Dict[str, Any],
                  deadline: Optional[float] = None,
                  max_retries: int = 3) -> Optional[Dict]:
        """Sign and fetch one hidden-API request. Returns parsed JSON or None.

        ``deadline`` (an absolute ``time.time()``) makes the caller's ``timeout=``
        a real wall-clock cap: signing, warm-up navigations and the retry loop
        all bail once it passes, so a flagged session can't drag a call far
        beyond the budget. ``max_retries`` bounds the empty-body retry loop —
        pass 1 for a quick probe (e.g. healthcheck) instead of 5 noisy attempts.
        """
        # Reset per-fetch so last_failure always reflects THIS attempt, never a
        # stale failure left over from a prior call (which would make the
        # facade _api_guard false-raise on a later genuine-empty result).
        self.last_failure = None
        path = HIDDEN_API_ENDPOINTS[endpoint_key]
        base_url = TIKTOK_BASE + path

        if deadline is not None and time.time() > deadline:
            logger.warning("[%s] already past timeout — not starting", endpoint_key)
            self.last_failure = "timeout"
            return None

        if not wait_for_acrawler(self.session, attempts=5, deadline=deadline):
            if deadline is not None and time.time() > deadline:
                logger.warning("[%s] signing wait hit timeout", endpoint_key)
                self.last_failure = self.last_failure or "timeout"
            else:
                logger.error("byted_acrawler never loaded; cannot sign requests")
                self.last_failure = "signing"
            return None

        base_params = self._ensure_base_params()

        def _expected_keys(key: str) -> List[str]:
            if key == "user_detail":     return ["userInfo"]
            if key == "hashtag_detail":  return ["challengeInfo"]
            if key == "video_comments":  return ["comments"]
            if key == "search_keyword":    return ["user_list"]
            if key == "search_general":  return ["data", "item_list", "itemList"]
            if key == "sound_detail":    return ["musicInfo", "music"]
            if key == "playlist_detail": return ["mixInfo", "mix"]
            return ["itemList"]

        max_retries = max(1, int(max_retries))
        sleep_range = (1.0, 2.0)

        for attempt in range(1, max_retries + 1):
            if deadline is not None and time.time() > deadline:
                logger.warning("[%s] hit timeout before attempt %d — returning", endpoint_key, attempt)
                self.last_failure = self.last_failure or "timeout"
                return None
            try:
                ms_token = self.session.get_ms_token() or ""
                merged = {**base_params, **params, "msToken": ms_token}

                if endpoint_key == "trending":
                    merged.setdefault("from_page", "fyp")
                elif endpoint_key in {"related_videos", "video_comments"}:
                    merged.setdefault("from_page", "video")
                # hashtag_videos: intentionally NO from_page override.
                # The base_params default ("from_page=user") is what TikTok
                # returns items for; "challenge" triggers statusCode=100002.

                query = urlencode(merged, safe="=", quote_via=quote)
                full_url = f"{base_url}?{query}"

                try:
                    x_bogus = frontier_sign(self.session, full_url)
                except SigningFailed as exc:
                    logger.warning("Signing failed: %s", exc)
                    x_bogus = ""

                signed_url = full_url + (f"&X-Bogus={x_bogus}" if x_bogus else "")
                result = fetch_in_page(self.session, signed_url)
                if result is None:
                    status, body = "?", ""
                else:
                    status, body = result
                logger.info("hidden_api %s -> HTTP %s, body length %d",
                            endpoint_key, status, len(body or ""))

                if not body or len(body) < 10:
                    if attempt < max_retries:
                        delay = random.uniform(*sleep_range)
                        logger.warning("[%s] empty body (attempt %d/%d). Sleeping %.1fs",
                                       endpoint_key, attempt, max_retries, delay)
                        # Warm up once on the first empty only. A full /foryou re-nav is
                        # ~5.5s and repeating it per retry doesn't unstick a flagged IP.
                        if attempt == 1:
                            try:
                                self.session.go(TIKTOK_BASE + "/foryou", wait=2.5)
                            except Exception:
                                pass
                        time.sleep(delay)
                        continue
                    logger.warning(
                        "[%s] TikTok returned empty responses after %d attempts. "
                        "This usually means bot detection. Try: "
                        "(1) headless=False, (2) waiting a few minutes before "
                        "retrying the same target, (3) refreshing your login "
                        "with pyk.login().",
                        endpoint_key, max_retries,
                    )
                    self.last_failure = "challenged"
                    return None

                data = json.loads(body)

                if data.get("statusCode") == 0 or any(k in data for k in _expected_keys(endpoint_key)):
                    self.last_failure = None   # success
                    return data

                if endpoint_key == "hashtag_videos" and data.get("statusCode") == 100002:
                    logger.warning("[%s] API blocked (statusCode=100002)", endpoint_key)
                    self.last_failure = "status:100002"
                    return None

                if attempt < max_retries:
                    delay = random.uniform(*sleep_range)
                    logger.warning("[%s] statusCode=%s (attempt %d/%d)",
                                   endpoint_key, data.get("statusCode"), attempt, max_retries)
                    time.sleep(delay)
                    continue
                self.last_failure = f"status:{data.get('statusCode')}"
                return None

            except json.JSONDecodeError as exc:
                if attempt < max_retries:
                    time.sleep(random.uniform(*sleep_range))
                    continue
                logger.error("[%s] JSON decode failed: %s", endpoint_key, exc)
                self.last_failure = "decode"
                return None
            except Exception as exc:
                if attempt < max_retries:
                    time.sleep(random.uniform(*sleep_range))
                    continue
                logger.error("[%s] fetch failed: %s", endpoint_key, exc)
                self.last_failure = "error"
                return None

        return None

    # ---- caches --------------------------------------------------------
    def _cache_set(self, key: str, value: Any) -> None:
        if value:
            self._last_ok[key] = value

    def _cache_get(self, key: str, default: Any = None) -> Any:
        return self._last_ok.get(key, default)

    # ---- health --------------------------------------------------------
    _HEALTH_HINTS = {
        "signing":    "byted_acrawler never loaded — a fresh/headless/blocked session can't "
                      "sign requests. Warm the persistent profile, or switch network / re-login.",
        "challenged": "TikTok returned empty bodies after retries — this IP is being "
                      "bot-challenged. Switch network (hotspot/VPN) or wait a few hours, then re-login.",
        "decode":     "TikTok returned non-JSON (often a challenge/redirect page).",
        "error":      "Network/fetch error talking to TikTok.",
        "empty":      "Signed fetch returned no data (cause unknown).",
    }

    def healthcheck(self, probe_user: str = "tiktok") -> Dict[str, Any]:
        """Sign one known request and report whether the signed-API path works.

        Returns ``{"ok": bool, "reason": str|None, "detail": str}``. Run this
        BEFORE a collection so a flagged IP, a cold/headless session, or an
        expired login surfaces immediately — instead of as silent empty CSVs.
        """
        self.last_failure = None
        # One quick attempt — a probe shouldn't burn 5 retries + warm-up navigations.
        resp = self.fetch_api("user_detail",
                              {"uniqueId": normalize_username(probe_user), "secUid": ""},
                              max_retries=1)
        if resp:
            nick = safe(resp, "userInfo", "user", "nickname", default="")
            return {"ok": True, "reason": None,
                    "detail": f"signed fetch OK (@{probe_user} -> {nick!r})"}
        reason = self.last_failure or "empty"
        if reason.startswith("status:"):
            detail = f"endpoint returned {reason} (may be endpoint-specific, not the whole API)"
        else:
            detail = self._HEALTH_HINTS.get(reason, self._HEALTH_HINTS["empty"])
        return {"ok": False, "reason": reason, "detail": detail}

    # ---- endpoint methods ---------------------------------------------
    def get_user_info(self, username: str) -> Optional[Dict]:
        username = normalize_username(username)
        resp = self.fetch_api("user_detail", {"uniqueId": username, "secUid": ""})
        self._cache_set(f"user_detail:{username}", resp)
        return resp

    def get_user_videos(self, username: str, count: int = 30,
                        timeout: Optional[float] = None) -> List[Dict]:
        username = normalize_username(username)
        user_resp = self.get_user_info(username)
        sec_uid = safe(user_resp or {}, "userInfo", "user", "secUid", default="")
        if not user_resp or not sec_uid:
            logger.warning("user_detail unavailable for %s", username)
            return []

        deadline = (time.time() + timeout) if timeout else None
        videos: List[Dict] = []
        cursor = 0
        failures = 0
        seen_cursors: set = set()
        page = 0

        while len(videos) < count:
            if deadline and time.time() > deadline:
                logger.warning("user_videos hit timeout for %s — returning %d", username, len(videos))
                break
            resp = self.fetch_api("user_videos", {
                "secUid": sec_uid, "count": 35, "cursor": cursor,
            }, deadline=deadline)
            if not resp:
                failures += 1
                if failures >= 4:
                    logger.warning("user_videos failed %d times; returning %d", failures, len(videos))
                    return videos[:count]
                # keep _base_params stable — re-deriving rotates device_id mid-session (bot signal)
                try:
                    self.session.go(f"{TIKTOK_BASE}/@{username}", wait=2.5)
                except Exception:
                    pass
                time.sleep(random.uniform(3, 6))
                continue

            failures = 0
            page += 1
            for item in resp.get("itemList") or []:
                videos.append(item)
                if len(videos) >= count:
                    break
            logger.info("user_videos page=%d collected=%d/%d", page, len(videos), count)
            if not resp.get("hasMore", False):
                break

            next_cursor = resp.get("cursor", 0)
            try:
                next_cursor = int(next_cursor)
            except Exception:
                break
            if next_cursor in seen_cursors or next_cursor == cursor:
                logger.info("user_videos cursor stalled at %s — stopping", next_cursor)
                break
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            time.sleep(random.uniform(1.0, 2.2))

        self._cache_set(f"user_videos:{username}", videos[:count])
        return videos[:count]

    def get_hashtag_info(self, hashtag: str) -> Optional[Dict]:
        """Hashtag/challenge detail (id, stats). Standalone counterpart to the
        lookup get_hashtag_videos does internally."""
        hashtag = normalize_hashtag(hashtag)
        resp = self.fetch_api("hashtag_detail", {"challengeName": hashtag})
        self._cache_set(f"hashtag_detail:{hashtag}", resp)
        return resp

    def get_hashtag_videos(self, hashtag: str, count: int = 30,
                          timeout: Optional[float] = None) -> List[Dict]:
        hashtag = normalize_hashtag(hashtag)
        detail = self.fetch_api("hashtag_detail", {"challengeName": hashtag})
        if not detail:
            logger.warning("hashtag_detail unavailable for %s", hashtag)
            return []
        challenge_id = safe(detail, "challengeInfo", "challenge", "id", default="")
        if not challenge_id:
            logger.warning("no challengeID for #%s", hashtag)
            return []

        # Warm up on the hashtag's own page first — TikTok grants the request
        # context expected by /api/challenge/item_list/.
        try:
            self.session.go(f"{TIKTOK_BASE}/tag/{hashtag}", wait=3.0)
        except Exception:
            pass

        deadline = (time.time() + timeout) if timeout else None
        videos: List[Dict] = []
        cursor = 0
        blocked = 0
        while len(videos) < count:
            if deadline and time.time() > deadline:
                logger.warning("hashtag_videos hit timeout for #%s — returning %d", hashtag, len(videos))
                break
            resp = self.fetch_api("hashtag_videos", {
                "challengeID": challenge_id,
                "count": 30,
                "cursor": cursor,
            }, deadline=deadline)
            if not resp:
                blocked += 1
                if blocked > 3:
                    logger.warning("hashtag_videos blocked; trying search_videos fallback")
                    fb = self.search_videos(f"#{hashtag}", count) or self.search_videos(hashtag, count)
                    return (videos + fb)[:count]
                # keep _base_params stable — re-deriving rotates device_id mid-session (bot signal)
                try:
                    self.session.go(f"{TIKTOK_BASE}/tag/{hashtag}", wait=3.0)
                except Exception:
                    pass
                time.sleep(random.uniform(2, 4))
                continue

            blocked = 0
            for item in resp.get("itemList") or []:
                videos.append(item)
                if len(videos) >= count:
                    break
            if not resp.get("hasMore", False):
                break
            cursor = resp.get("cursor", 0)

        self._cache_set(f"hashtag_videos:{hashtag}", videos[:count])
        return videos[:count]

    def _paginate_video_comments(
        self, vid: str, count: int, sort_type: int,
        seen_cids: set, comments: List[Dict],
        deadline: Optional[float] = None,
    ) -> None:
        """One pagination pass over /api/comment/list/ for a given sort_type.
        Appends to `comments` and updates `seen_cids` in place. If ``deadline``
        (an absolute time.time() value) is set, stops and returns the partial
        result once it passes — no single video can hang the run."""
        seen_cursors: set = {0}
        cursor = 0
        failures = 0
        page = 0

        while len(comments) < count:
            if deadline is not None and time.time() > deadline:
                logger.warning("video_comments vid=%s sort=%d hit timeout — stopping at %d",
                               vid, sort_type, len(comments))
                return
            resp = self.fetch_api("video_comments", {
                "aweme_id": vid, "count": 20, "cursor": cursor,
                "sort_type": sort_type,
            }, deadline=deadline)
            if not resp:
                # fetch_api bailed on the deadline -> stop now with the partial,
                # don't burn the failure-retry sleeps past the budget.
                if deadline is not None and time.time() > deadline:
                    logger.warning("video_comments vid=%s sort=%d hit timeout — stopping at %d",
                                   vid, sort_type, len(comments))
                    return
                failures += 1
                if failures >= 3:
                    logger.warning("video_comments failed %d times for %s (sort=%d); stopping pass at %d",
                                   failures, vid, sort_type, len(comments))
                    return
                time.sleep(random.uniform(2.0, 4.0))
                continue
            failures = 0
            page += 1

            new_this_page = 0
            for c in (resp.get("comments") or []):
                cid = c.get("cid")
                if cid and cid in seen_cids:
                    continue
                if cid:
                    seen_cids.add(cid)
                comments.append(c)
                new_this_page += 1
                if len(comments) >= count:
                    break

            logger.info("video_comments vid=%s sort=%d page=%d collected=%d/%d new=%d",
                        vid, sort_type, page, len(comments), count, new_this_page)

            if len(comments) >= count:
                return
            if not resp.get("has_more", False):
                return
            if new_this_page == 0:
                logger.info("video_comments vid=%s sort=%d page returned 0 new — stopping pass",
                            vid, sort_type)
                return

            next_cursor = resp.get("cursor", 0)
            try:
                next_cursor = int(next_cursor)
            except Exception:
                return
            if next_cursor in seen_cursors:
                logger.info("video_comments vid=%s sort=%d cursor stalled at %s — stopping pass",
                            vid, sort_type, next_cursor)
                return
            seen_cursors.add(next_cursor)
            cursor = next_cursor
            time.sleep(random.uniform(0.6, 1.4))

    def get_video_comments(self, video_url_or_id: str, count: int = 30,
                           dual_sort: bool = True,
                           timeout: Optional[float] = None) -> List[Dict]:
        """Fetch up to ``count`` unique comments for a video.

        TikTok's /api/comment/list/ caps depth per sort order (~500–6000).
        With ``dual_sort=True`` (default), we run two paginations — top-first
        (sort_type=0) then newest-first (sort_type=1) — and dedup by cid.
        This typically unlocks 1.5–2× more unique comments on viral videos.

        ``timeout`` (seconds) bounds the WHOLE call: pagination stops and
        returns whatever was collected once the budget is spent, so no single
        video can hang a batch run. ``None`` = no limit (legacy behavior).
        """
        vid = extract_video_id(video_url_or_id) or str(video_url_or_id)
        comments: List[Dict] = []
        seen_cids: set = set()
        deadline = (time.time() + timeout) if timeout else None

        sort_orders = (0, 1) if dual_sort else (0,)
        for sort_type in sort_orders:
            if len(comments) >= count:
                break
            if deadline is not None and time.time() > deadline:
                break
            before = len(comments)
            self._paginate_video_comments(vid, count, sort_type, seen_cids, comments,
                                          deadline=deadline)
            logger.info("video_comments vid=%s sort=%d pass complete: +%d new (total %d/%d)",
                        vid, sort_type, len(comments) - before, len(comments), count)

        self._cache_set(f"video_comments:{vid}", comments[:count])
        return comments[:count]

    def get_comment_replies(self, comment_id: str, video_id: str = "",
                            count: int = 50) -> List[Dict]:
        """Replies under one comment, via the signed /api/comment/list/reply/."""
        replies: List[Dict] = []
        seen: set = set()
        seen_cursors: set = {0}
        cursor = 0
        while len(replies) < count:
            resp = self.fetch_api("comment_replies", {
                "comment_id": str(comment_id),
                "item_id": str(extract_video_id(video_id) or video_id),
                "count": 20, "cursor": cursor,
            })
            if not resp:
                break
            batch = resp.get("comments") or []
            new = 0
            for c in batch:
                cid = c.get("cid")
                if cid and cid in seen:
                    continue
                if cid:
                    seen.add(cid)
                replies.append(c)
                new += 1
                if len(replies) >= count:
                    break
            if new == 0 or not resp.get("has_more", False):
                break
            nxt = resp.get("cursor", 0)
            try:
                nxt = int(nxt)
            except Exception:
                break
            if nxt in seen_cursors:
                break
            seen_cursors.add(nxt)
            cursor = nxt
            time.sleep(random.uniform(0.6, 1.4))
        return replies[:count]

    def get_related_videos(self, video_url_or_id: str, count: int = 16) -> List[Dict]:
        vid = extract_video_id(video_url_or_id) or str(video_url_or_id)
        if "/video/" in str(video_url_or_id):
            try:
                self.session.go(str(video_url_or_id), wait=2.0)
            except Exception:
                pass
        resp = self.fetch_api("related_videos", {"itemID": vid, "count": count})
        items = ((resp or {}).get("itemList") or [])[:count]
        self._cache_set(f"related_videos:{vid}", items)
        return items

    def get_trending_videos(self, count: int = 30) -> List[Dict]:
        # Trending endpoint returns ~10 per call; paginate to satisfy larger counts.
        items: List[Dict] = []
        for _ in range(max(1, (count + 9) // 10)):
            resp = self.fetch_api("trending", {"from_page": "fyp", "count": 10})
            if not resp:
                break
            new = resp.get("itemList") or []
            if not new:
                break
            items.extend(new)
            if len(items) >= count:
                break
            time.sleep(random.uniform(0.8, 1.6))
        self._cache_set("trending", items[:count])
        return items[:count]

    def search_keyword(self, keyword: str, count: int = 10) -> List[Dict]:
        """Search TikTok for users matching `keyword`. Paginates until count.

        TikTok's `/api/search/user/full/` NOW requires the
        `web_search_code` magic blob (same one used by the general search
        endpoint) and a `search_id` for pagination consistency.
        """
        web_search_code = (
            '{"tiktok":{"client_params_x":{"search_engine":'
            '{"ies_mt_user_live_video_card_use_libra":1,'
            '"mt_search_general_user_live_card":1}},"search_server":{}}}'
        )
        users: List[Dict] = []
        cursor = 0
        search_id = ""
        while len(users) < count:
            params = {
                "keyword":         keyword,
                "cursor":          cursor,
                "from_page":       "search",
                "web_search_code": web_search_code,
            }
            if search_id:
                params["search_id"] = search_id
            resp = self.fetch_api("search_keyword", params)
            if not resp:
                break
            user_list = resp.get("user_list") or []
            for u in user_list:
                info = u.get("user_info", {}) if isinstance(u, dict) else {}
                if info:
                    users.append(info)
                if len(users) >= count:
                    break
            if not resp.get("has_more", False):
                break
            try:
                next_cursor = int(resp.get("cursor", 0))
            except Exception:
                break
            if next_cursor == cursor:
                break
            cursor = next_cursor
            # Carry the search_id forward so TikTok serves a consistent
            # paginated result set (per reference impl).
            search_id = resp.get("rid", "") or search_id
            time.sleep(random.uniform(0.8, 1.6))
        self._cache_set(f"search_keyword:{keyword}", users[:count])
        return users[:count]

    def search_videos(self, keyword: str, count: int = 30,
                     timeout: Optional[float] = None) -> List[Dict]:
        web_search_code = (
            '{"tiktok":{"client_params_x":{"search_engine":'
            '{"ies_mt_user_live_video_card_use_libra":1,'
            '"mt_search_general_user_live_card":1}},"search_server":{}}}'
        )
        deadline = (time.time() + timeout) if timeout else None

        def _extract(resp: Dict[str, Any]) -> List[Dict[str, Any]]:
            out: List[Dict[str, Any]] = []
            for item in resp.get("item_list", []) or []:
                if isinstance(item, dict):
                    out.append(item)
            for item in resp.get("itemList") or []:
                if isinstance(item, dict):
                    out.append(item)
            for entry in resp.get("data", []) or []:
                if not isinstance(entry, dict):
                    continue
                cand = entry.get("item") or entry.get("item_info") or entry.get("itemInfo")
                if isinstance(cand, dict):
                    out.append(cand)
            return out

        videos: List[Dict] = []
        cursor = 0
        while len(videos) < count:
            if deadline and time.time() > deadline:
                break
            resp = self.fetch_api("search_general", {
                "keyword":         keyword,
                "cursor":          cursor,
                "from_page":       "search",
                "web_search_code": web_search_code,
            }, deadline=deadline)
            if not resp:
                break
            for item in _extract(resp):
                videos.append(item)
                if len(videos) >= count:
                    break
            has_more = bool(resp.get("has_more", False) or resp.get("hasMore", False))
            if not has_more:
                break
            try:
                next_cursor = int(resp.get("cursor", 0))
            except Exception:
                next_cursor = 0
            if next_cursor == cursor:
                break
            cursor = next_cursor

        return videos[:count]

    def get_sound_info(self, sound_id: str) -> Optional[Dict]:
        sound_id = extract_sound_id(sound_id) or str(sound_id)
        resp = self.fetch_api("sound_detail", {"musicId": str(sound_id)})
        self._cache_set(f"sound_detail:{sound_id}", resp)
        return resp

    def get_sound_videos(self, sound_id: str, count: int = 30,
                        timeout: Optional[float] = None) -> List[Dict]:
        sound_id = extract_sound_id(sound_id) or str(sound_id)
        deadline = (time.time() + timeout) if timeout else None
        videos: List[Dict] = []
        cursor = 0
        while len(videos) < count:
            if deadline and time.time() > deadline:
                break
            resp = self.fetch_api("sound_videos", {
                "musicID": str(sound_id), "count": 30, "cursor": cursor,
            }, deadline=deadline)
            if not resp:
                break
            for item in resp.get("itemList") or []:
                videos.append(item)
                if len(videos) >= count:
                    break
            if not resp.get("hasMore", False):
                break
            cursor = resp.get("cursor", 0)
        self._cache_set(f"sound_videos:{sound_id}", videos[:count])
        return videos[:count]

    def get_playlist_info(self, playlist_id: str) -> Optional[Dict]:
        playlist_id = extract_playlist_id(playlist_id) or str(playlist_id)
        resp = self.fetch_api("playlist_detail", {"mixId": str(playlist_id)})
        self._cache_set(f"playlist_detail:{playlist_id}", resp)
        return resp

    def get_playlist_videos(self, playlist_id: str, count: int = 30) -> List[Dict]:
        playlist_id = extract_playlist_id(playlist_id) or str(playlist_id)
        videos: List[Dict] = []
        cursor = 0
        while len(videos) < count:
            resp = self.fetch_api("playlist_videos", {
                "mixId": str(playlist_id), "count": 30, "cursor": cursor,
            })
            if not resp:
                break
            for item in resp.get("itemList") or []:
                videos.append(item)
                if len(videos) >= count:
                    break
            if not resp.get("hasMore", False):
                break
            cursor = resp.get("cursor", 0)
        self._cache_set(f"playlist_videos:{playlist_id}", videos[:count])
        return videos[:count]

    # ---- comment CSV helper -------------------------------------------
    def save_video_comments_csv(self, video_url_or_id: str, count: int, filename: str = "",
                                timeout: Optional[float] = None) -> str:
        vid = extract_video_id(video_url_or_id) or str(video_url_or_id)
        if not filename:
            filename = default_endpoint_filename("video_comments", vid)
        comments = self.get_video_comments(video_url_or_id, count, timeout=timeout)
        if not comments:
            comments = self._cache_get(f"video_comments:{vid}", [])
        if not comments:
            pd.DataFrame(columns=COMMENT_EXPORT_COLUMNS).to_csv(filename, index=False)
            logger.warning("No comments found for %s — wrote header-only CSV → %s", vid, filename)
            return filename
        rows = [comment_export_row(c, vid, engine="api") for c in comments]
        df = pd.DataFrame(rows, columns=COMMENT_EXPORT_COLUMNS)
        combined = deduplicate(filename, df, key="cid")
        combined.to_csv(filename, index=False)
        logger.info("Saved %d comments → %s", len(rows), filename)
        return filename
