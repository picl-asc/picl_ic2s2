"""tikwm.com fallback engine — no signing required.

WHY: pyktok_2026's primary path signs TikTok's hidden API directly. When the
signed API gets capped (e.g. /api/comment/list/ returns has_more=False after
~500-6000 comments on viral videos) or blocked, tikwm acts as a public
mirror that already did the signing for us. Plain HTTPS GET, JSON out, no
Playwright needed.

LIMITS — read these before you scale this up:
  - Soft throttle: ~1 request/second per IP. Going faster gets HTTP 429 or
    code=-1 with msg containing "rate"/"limit". This module sleeps
    1.0-2.0s between paginated calls.
  - Daily cap: roughly ~5000 calls/day per IP on the free endpoint.
    378 videos x ~30 comment pages each = ~11000 calls — you WILL hit the
    daily cap. Plan to either (a) split across days, (b) split across IPs
    via VPN/proxy rotation, or (c) only fall back to tikwm for the videos
    whose primary-API scrape capped early.
  - On rate-limit: tikwm enforces a rolling 24-hour window. Catch
    TikwmRateLimit, sleep until (start_time + 24h), resume from the last
    unfinished video. The resumable comment-loop in the demo notebook
    already does this idempotently — same skip-if-exists logic works.
  - Cloudflare: tikwm sits behind Cloudflare. From most consumer IPs a
    plain requests.get() works fine. If you get HTTP 403 / "Just a
    moment..." HTML, switch transport to the active Playwright context
    via TikwmEngine(use_playwright=True) — it reuses the browser's TLS
    fingerprint to clear the challenge.

THIS IS A PUBLIC MIRROR. Don't send auth-bearing requests through it.
"""
from __future__ import annotations

import json
import random
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple
from urllib.parse import urlencode

import requests

from ._logging import get_logger
from .exceptions import Throttled
from .targets import extract_sound_id, extract_video_id

logger = get_logger("tikwm")

# ---- rate limiting --------------------------------------------------------
# tikwm reports the per-IP request budget on EVERY response, via headers:
#   X-Limit-Request-Remaining  — requests left in the current window
#   X-Limit-Request-Reset      — seconds until the window resets
# We read those (server truth, accurate across processes/scripts on one IP)
# rather than a local counter — a local count can't know how many machines/IPs
# you run across, so it would only mislead. A fresh window is ~10,000 requests.
TIKWM_DAILY_CAP = 10000   # documented ceiling only; the server header is authoritative
_QUOTA_LOCK = threading.Lock()
_last_limit: Dict[str, Any] = {"remaining": None, "reset": None}


class TikwmRateLimit(Throttled):
    """Raised when tikwm reports the per-IP request budget is exhausted, or
    rejects a request as rate-limited."""


def _record_limit_headers(headers) -> None:
    """Capture tikwm's X-Limit-Request-* headers (server-reported, per IP)."""
    try:
        rem = headers.get("X-Limit-Request-Remaining")
        rst = headers.get("X-Limit-Request-Reset")
    except Exception:
        return
    with _QUOTA_LOCK:
        if rem is not None:
            try: _last_limit["remaining"] = int(rem)
            except (TypeError, ValueError): pass
        if rst is not None:
            try: _last_limit["reset"] = int(rst)
            except (TypeError, ValueError): pass


def tikwm_quota_remaining() -> Optional[int]:
    """Requests left in tikwm's current per-IP window, as tikwm reported it on the
    LAST response (None until the first request). This is server truth — not a
    local estimate — so it's accurate even across separate processes on one IP."""
    with _QUOTA_LOCK:
        return _last_limit["remaining"]


def tikwm_quota_reset_seconds() -> Optional[int]:
    """Seconds until tikwm's per-IP window resets, from the last response (or None)."""
    with _QUOTA_LOCK:
        return _last_limit["reset"]


def tikwm_to_itemstruct(v: Dict[str, Any]) -> Dict[str, Any]:
    """Reshape a tikwm video dict into TikTok itemStruct shape so the
    existing pyktok_2026._csv.generate_data_row() works unchanged.

    Mapping is best-effort — fields tikwm doesn't return (poi, stickers,
    geofencing) become empty strings, matching what generate_data_row already
    produces when the signed API omits them.
    """
    author = v.get("author") or {}
    return {
        "id":              v.get("video_id") or v.get("id", ""),
        "createTime":      v.get("create_time", 0),
        "video": {
            "duration":     v.get("duration", ""),
            "downloadAddr": v.get("hdplay") or v.get("play", ""),
            "playAddr":     v.get("play", ""),
        },
        "stats": {
            "diggCount":    v.get("digg_count", ""),
            "shareCount":   v.get("share_count", ""),
            "commentCount": v.get("comment_count", ""),
            "playCount":    v.get("play_count", ""),
        },
        "desc":            v.get("title") or v.get("desc", ""),
        "isAd":            v.get("is_ad", False),
        "stickersOnItem":  [],
        "author": {
            "uniqueId":    author.get("unique_id", ""),
            "nickname":    author.get("nickname", ""),
            "verified":    author.get("verified", False),
        },
        "authorStats": {
            "followerCount":  author.get("follower_count", ""),
            "followingCount": author.get("following_count", ""),
            "heartCount":     author.get("heart_count", ""),
            "videoCount":     author.get("aweme_count", ""),
            "diggCount":      author.get("digg_count", ""),
        },
        "locationCreated": v.get("region", ""),
        "poi": {"name": "", "address": "", "city": ""},
    }


def tikwm_video_filename(v: Dict[str, Any]) -> str:
    """Media filename matching the DOM/API scheme: <author>_<vid>.mp4."""
    from ._csv import media_filename
    author = (v.get("author") or {}).get("unique_id", "")
    vid    = v.get("video_id") or v.get("id") or "unknown"
    return media_filename(author, vid)

BASE = "https://www.tikwm.com/api"

_DEFAULT_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
}


def _looks_rate_limited(code: int, msg: str) -> bool:
    if code == 429 or code == -1 and any(k in msg.lower() for k in ("rate", "limit", "quota", "too many")):
        return True
    return False


def _looks_like_data_error(msg: str) -> bool:
    """A deterministic, input-level tikwm error (bad/empty/unresolvable id) —
    retrying the same params won't change it, so we fail fast instead of burning
    retries + daily quota. e.g. 'Comment info parsing is failed! Please check
    comment_id.', 'Url parsing is failed', 'not found'."""
    m = msg.lower()
    return any(k in m for k in ("parsing is failed", "please check", "not found",
                                "invalid", "does not exist", "no data"))


class TikwmEngine:
    """Mirror-backed counterpart to APIEngine. Same method names where possible.

    Parameters
    ----------
    use_playwright : bool
        Route requests through the active pyktok Playwright BrowserContext
        instead of `requests`. Use this if plain HTTP returns Cloudflare
        challenge pages from your IP.

    THREAD-SAFETY: one instance is NOT safe to share across threads — it holds
    a single requests.Session and a mutable ``last_call_rate_limited`` flag. For
    parallel collection, create ONE TikwmEngine per worker thread. And note the
    real throughput lever is IP rotation (tikwm caps ~1 req/s, ~5000/day per IP),
    not thread count — many threads on one IP just hit the rate limit faster.
    """

    def __init__(self, use_playwright: bool = False):
        self.use_playwright = use_playwright
        self._sess = requests.Session()
        self._sess.headers.update(_DEFAULT_HEADERS)
        # Set to True by paginated callers (get_video_comments, get_user_videos,
        # etc.) when a TikwmRateLimit was caught mid-pagination after some
        # data was already collected. Callers can write that partial data to
        # disk, then re-raise the exception so the outer loop knows to stop.
        self.last_call_rate_limited: bool = False

    # ---- transport ----------------------------------------------------
    def _get(self, path: str, params: Dict[str, Any], retries: int = 3) -> Dict:
        url = f"{BASE}/{path.lstrip('/')}"
        last_err: Optional[str] = None

        # If tikwm told us on a PRIOR response that the per-IP budget is spent,
        # stop before spending another request (don't discard a good response).
        with _QUOTA_LOCK:
            rem = _last_limit["remaining"]
            rst = _last_limit["reset"]
        if rem is not None and rem <= 0:
            raise TikwmRateLimit(
                "tikwm reports 0 requests remaining for this IP"
                + (f" (resets in ~{rst}s)" if rst else "")
                + ". Rotate IP (VPN/hotspot) or wait for the window to reset."
            )

        for attempt in range(1, retries + 1):
            try:
                if self.use_playwright:
                    j = self._get_via_playwright(url, params)
                else:
                    r = self._sess.get(url, params=params, timeout=20)
                    _record_limit_headers(r.headers)   # server-reported budget
                    if r.status_code in (429,):
                        raise TikwmRateLimit(f"tikwm HTTP 429 on {path}")
                    if r.status_code >= 500:
                        last_err = f"HTTP {r.status_code}"
                        time.sleep(random.uniform(1.5, 3.5))
                        continue
                    if r.status_code == 403:
                        # Cloudflare blocks plain HTTP intermittently — retry
                        # through the browser context if we have one (F13).
                        j = self._via_playwright_or_raise(url, params, path)
                    else:
                        j = r.json()
            except TikwmRateLimit:
                raise
            except Exception as exc:
                last_err = str(exc)
                if attempt < retries:
                    time.sleep(random.uniform(1.5, 3.5))
                    continue
                raise RuntimeError(f"tikwm {path} transport failure: {last_err}")

            code = j.get("code", -999)
            msg = j.get("msg", "")
            if code == 0:
                return j.get("data") or {}
            if _looks_rate_limited(code, msg):
                raise TikwmRateLimit(f"tikwm rate-limited on {path}: {msg}")
            last_err = f"code={code} msg={msg}"
            # Deterministic input error (bad/empty id) — retrying is pointless;
            # fail fast so the caller can handle it.
            if _looks_like_data_error(msg):
                raise RuntimeError(f"tikwm {path} error: {last_err}")
            if attempt < retries:
                time.sleep(random.uniform(1.5, 3.5))
                continue
            raise RuntimeError(f"tikwm {path} error after {retries} tries: {last_err}")

        raise RuntimeError(f"tikwm {path}: exhausted retries ({last_err})")

    def _via_playwright_or_raise(self, url: str, params: Dict[str, Any], path: str) -> Dict:
        """On a Cloudflare 403, retry through the active browser context if one
        exists (F13); otherwise raise a clear, actionable error."""
        try:
            from . import facade as _f
            have_session = _f._pyk is not None
        except Exception:
            have_session = False
        if have_session:
            logger.warning("tikwm 403 (Cloudflare) on %s — retrying via the browser context", path)
            try:
                return self._get_via_playwright(url, params)
            except Exception as exc:
                # The browser context often hits the same Cloudflare wall on this
                # endpoint (returns an HTML challenge, not JSON) — fall through to
                # the clean, actionable error instead of a raw JSONDecodeError.
                logger.warning("tikwm browser-context retry also failed on %s (%s)",
                               path, type(exc).__name__)
        raise RuntimeError(
            "tikwm returned HTTP 403 (Cloudflare) — this endpoint is intermittently blocked. "
            "For a user's videos, the reliable paths are pyk.save_user(username) (yt-dlp) or "
            "mode='api' after login."
        )

    def _get_via_playwright(self, url: str, params: Dict[str, Any]) -> Dict:
        from .facade import _get_pyk  # local import to avoid circular
        ctx = _get_pyk().session._context
        full = f"{url}?{urlencode(params)}"
        resp = ctx.request.get(full, timeout=20000)
        try:
            _record_limit_headers(resp.headers)
        except Exception:
            pass
        try:
            return resp.json()
        except Exception as exc:
            # Cloudflare served HTML (a challenge page), not JSON — surface a clean
            # error instead of a raw JSONDecodeError bubbling to the user.
            raise RuntimeError(
                f"tikwm via browser returned non-JSON (HTTP {getattr(resp, 'status', '?')}) — likely Cloudflare"
            ) from exc

    # ---- field normalizers --------------------------------------------
    @staticmethod
    def _normalize_comment(c: Dict[str, Any]) -> Dict[str, Any]:
        """Map tikwm comment fields onto the names comment_export_row expects."""
        out = dict(c)
        if "cid" not in out and "id" in c:
            out["cid"] = c["id"]
        if "reply_comment_total" not in out and "reply_total" in c:
            out["reply_comment_total"] = c["reply_total"]
        if "aweme_id" not in out and "video_id" in c:
            out["aweme_id"] = c["video_id"]
        # user.uid is what comment_export_row reads
        user = out.get("user", {})
        if isinstance(user, dict) and "uid" not in user and "id" in user:
            user = {**user, "uid": user["id"]}
            out["user"] = user
        return out

    # ---- comments -----------------------------------------------------
    def get_video_comments(self, video_url_or_id: str, count: int = 30) -> List[Dict]:
        """Fetch up to `count` unique comments via tikwm. URL or ID accepted.

        On a TikwmRateLimit raised by `_get`, the function preserves any
        already-collected comments by setting `self.last_call_rate_limited
        = True` and returning the partial list. The caller is responsible
        for writing the partial CSV and then re-raising TikwmRateLimit
        (using `last_call_rate_limited` to detect this state) so the outer
        loop knows to stop.
        """
        self.last_call_rate_limited = False
        vid = extract_video_id(video_url_or_id) or str(video_url_or_id).strip()
        if str(video_url_or_id).startswith("http"):
            url = video_url_or_id
        else:
            url = f"https://www.tiktok.com/@x/video/{vid}"

        comments: List[Dict] = []
        seen_cids: set = set()
        seen_cursors: set = {"0"}
        cursor = "0"
        page = 0

        while len(comments) < count:
            page += 1
            try:
                data = self._get("/comment/list", {
                    "url": url,
                    "count": min(50, count - len(comments)),
                    "cursor": cursor,
                })
            except TikwmRateLimit:
                logger.warning("tikwm rate limit hit on vid=%s after %d comments",
                               vid, len(comments))
                self.last_call_rate_limited = True
                return comments[:count]

            batch = data.get("comments") or []
            new = 0
            for c in batch:
                cid = c.get("id") or c.get("cid")
                if cid and cid in seen_cids:
                    continue
                if cid:
                    seen_cids.add(cid)
                comments.append(self._normalize_comment(c))
                new += 1
                if len(comments) >= count:
                    break

            has_more = bool(data.get("hasMore") or data.get("has_more"))
            logger.info("tikwm comments vid=%s page=%d collected=%d/%d new=%d has_more=%s",
                        vid, page, len(comments), count, new, has_more)

            if len(comments) >= count or new == 0 or not has_more:
                break
            nxt = str(data.get("cursor", ""))
            if not nxt or nxt in seen_cursors:
                logger.info("tikwm comments vid=%s cursor stalled at %s — stopping", vid, nxt)
                break
            seen_cursors.add(nxt)
            cursor = nxt
            time.sleep(random.uniform(1.0, 2.0))

        return comments[:count]

    # ---- replies ------------------------------------------------------
    def get_comment_replies(self, comment_id: str, video_id: str, count: int = 50) -> List[Dict]:
        """Fetch replies to a single comment. tikwm: /api/comment/reply
        requires the numeric video_id (a URL is accepted and normalized) +
        comment_id. A comment with no replies (tikwm code=-1 'Comment info
        parsing is failed') returns [] rather than raising."""
        vid = extract_video_id(video_id) or str(video_id)
        out: List[Dict] = []
        cursor = "0"
        seen_cursors = {"0"}
        while len(out) < count:
            try:
                data = self._get("/comment/reply", {
                    "video_id": str(vid), "comment_id": str(comment_id),
                    "count": min(50, count - len(out)), "cursor": cursor,
                })
            except TikwmRateLimit:
                raise  # let resumable callers stop cleanly + keep partial
            except RuntimeError as exc:
                # tikwm couldn't resolve replies for this comment — almost always
                # "no replies" or an unfetchable/deleted comment. Degrade to the
                # partial we have instead of crashing the caller.
                logger.warning("tikwm /comment/reply (%s): %s — returning %d replies",
                               comment_id, exc, len(out))
                break
            batch = data.get("comments") or []
            if not batch:
                break
            for c in batch:
                out.append(self._normalize_comment(c))
                if len(out) >= count:
                    break
            if not (data.get("hasMore") or data.get("has_more")):
                break
            nxt = str(data.get("cursor", ""))
            if not nxt or nxt in seen_cursors:
                break
            seen_cursors.add(nxt)
            cursor = nxt
            time.sleep(random.uniform(1.0, 2.0))
        return out[:count]

    # ---- search -------------------------------------------------------
    def search_videos(self, keyword: str, count: int = 10) -> List[Dict]:
        """Keyword search → videos. tikwm: /api/feed/search."""
        out: List[Dict] = []
        cursor = "0"
        seen_cursors = {"0"}
        while len(out) < count:
            data = self._get("/feed/search", {
                "keywords": keyword, "count": min(20, count - len(out)), "cursor": cursor,
            })
            batch = data.get("videos") or []
            if not batch:
                break
            out.extend(batch)
            if not (data.get("hasMore") or data.get("has_more")):
                break
            nxt = str(data.get("cursor", ""))
            if not nxt or nxt in seen_cursors:
                break
            seen_cursors.add(nxt)
            cursor = nxt
            time.sleep(random.uniform(1.0, 2.0))
        return out[:count]

    def search_hashtags(self, keyword: str, count: int = 10) -> List[Dict]:
        """Find hashtags by keyword. tikwm: /api/challenge/search.
        Returns hashtag metadata (id, name, view_count) — NOT videos.
        Pair with get_hashtag_videos(challenge_id) to fetch videos."""
        out: List[Dict] = []
        cursor = "0"
        seen_cursors = {"0"}
        while len(out) < count:
            data = self._get("/challenge/search", {
                "keywords": keyword, "count": min(20, count - len(out)), "cursor": cursor,
            })
            batch = data.get("challenge_list") or []
            if not batch:
                break
            out.extend(batch)
            if not (data.get("hasMore") or data.get("has_more")):
                break
            nxt = str(data.get("cursor", ""))
            if not nxt or nxt in seen_cursors:
                break
            seen_cursors.add(nxt)
            cursor = nxt
            time.sleep(random.uniform(1.0, 2.0))
        return out[:count]

    # ---- hashtag ------------------------------------------------------
    def get_hashtag_info(self, name: str) -> Dict:
        """Hashtag/challenge detail by name. tikwm: /api/challenge/info."""
        return self._get("/challenge/info", {"challenge_name": name.lstrip("#")})

    def get_hashtag_videos(self, name_or_id: str, count: int = 20) -> List[Dict]:
        """Videos in a hashtag. tikwm: /api/challenge/posts.
        Accepts either a hashtag name (we'll resolve to challenge_id first)
        or a numeric challenge_id."""
        if str(name_or_id).isdigit():
            challenge_id = str(name_or_id)
        else:
            info = self.get_hashtag_info(name_or_id)
            challenge_id = str(info.get("ch_id") or info.get("id") or "")
            if not challenge_id:
                raise RuntimeError(f"tikwm: could not resolve hashtag '{name_or_id}' to challenge_id")

        out: List[Dict] = []
        cursor = "0"
        seen_cursors = {"0"}
        while len(out) < count:
            data = self._get("/challenge/posts", {
                "challenge_id": challenge_id,
                "count": min(20, count - len(out)),
                "cursor": cursor,
            })
            batch = data.get("videos") or []
            if not batch:
                break
            out.extend(batch)
            if not (data.get("hasMore") or data.get("has_more")):
                break
            nxt = str(data.get("cursor", ""))
            if not nxt or nxt in seen_cursors:
                break
            seen_cursors.add(nxt)
            cursor = nxt
            time.sleep(random.uniform(1.0, 2.0))
        return out[:count]

    # ---- music / sound ------------------------------------------------
    def get_sound_info(self, music_id_or_url: str) -> Dict:
        """Sound/music detail. tikwm: /api/music/info — takes url=, NOT music_id.
        Accepts a numeric id or any /music/ URL."""
        s = str(music_id_or_url)
        if s.startswith("http"):
            url = s
        else:
            sid = extract_sound_id(s) or s
            url = f"https://www.tiktok.com/music/x-{sid}"
        return self._get("/music/info", {"url": url})

    def get_sound_videos(self, music_id: str, count: int = 20) -> List[Dict]:
        """Videos using a sound. tikwm: /api/music/posts. Accepts id or /music/ URL."""
        music_id = extract_sound_id(music_id) or str(music_id)
        out: List[Dict] = []
        cursor = "0"
        seen_cursors = {"0"}
        while len(out) < count:
            data = self._get("/music/posts", {
                "music_id": str(music_id),
                "count": min(30, count - len(out)),
                "cursor": cursor,
            })
            batch = data.get("videos") or []
            if not batch:
                break
            out.extend(batch)
            if not (data.get("hasMore") or data.get("has_more")):
                break
            nxt = str(data.get("cursor", ""))
            if not nxt or nxt in seen_cursors:
                break
            seen_cursors.add(nxt)
            cursor = nxt
            time.sleep(random.uniform(1.0, 2.0))
        return out[:count]

    # ---- trending -----------------------------------------------------
    def get_trending_videos(self, count: int = 20, region: str = "US") -> List[Dict]:
        """Trending/FYP-style feed. tikwm /api/feed/list returns one ~20-item
        snapshot per call, so we call repeatedly and dedupe by id to reach
        `count` (trending overlaps a lot, so we stop when no new items arrive)."""
        out: List[Dict] = []
        seen: set = set()
        empty_rounds = 0
        while len(out) < count and empty_rounds < 3:
            data = self._get("/feed/list", {"region": region, "count": min(30, count - len(out))})
            batch = data if isinstance(data, list) else (data.get("videos") or [])
            new = 0
            for v in batch:
                vid = v.get("video_id") or v.get("id")
                if vid and vid in seen:
                    continue
                if vid:
                    seen.add(vid)
                out.append(v)
                new += 1
                if len(out) >= count:
                    break
            empty_rounds = empty_rounds + 1 if new == 0 else 0
            if len(out) < count:
                time.sleep(random.uniform(1.0, 2.0))
        return out[:count]

    # ---- user ---------------------------------------------------------
    def get_user_info(self, username: str) -> Dict:
        return self._get("/user/info", {"unique_id": username.lstrip("@")})

    def get_user_videos(self, username: str, count: int = 30) -> List[Dict]:
        username = username.lstrip("@")
        videos: List[Dict] = []
        cursor = "0"
        seen_cursors = {"0"}
        while len(videos) < count:
            data = self._get("/user/posts", {
                "unique_id": username,
                "count": min(35, count - len(videos)),
                "cursor": cursor,
            })
            batch = data.get("videos") or []
            if not batch:
                break
            videos.extend(batch)
            if not (data.get("hasMore") or data.get("has_more")):
                break
            nxt = str(data.get("cursor", ""))
            if not nxt or nxt in seen_cursors:
                break
            seen_cursors.add(nxt)
            cursor = nxt
            time.sleep(random.uniform(1.0, 2.0))
        return videos[:count]

    # ---- single post --------------------------------------------------
    def get_video(self, video_url: str, hd: bool = True) -> Dict:
        return self._get("/", {"url": video_url, "hd": "1" if hd else "0"})

    # ---- video download ----------------------------------------------
    def download_video(
        self,
        v: Dict[str, Any],
        dir_path: Optional[str] = None,
        hd: bool = True,
    ) -> Optional[str]:
        """Stream a tikwm video dict's MP4 to disk. Filename matches DOM:
        @<user>_video_<id>.mp4. Returns the path on success, None if no URL.
        """
        url = (v.get("hdplay") if hd else None) or v.get("play")
        if not url:
            return None
        target = Path(dir_path or ".") / tikwm_video_filename(v)
        target.parent.mkdir(parents=True, exist_ok=True)
        try:
            with self._sess.get(url, stream=True, timeout=60) as r:
                if r.status_code != 200:
                    logger.warning("tikwm download HTTP %d for %s", r.status_code, target.name)
                    return None
                with open(target, "wb") as f:
                    for chunk in r.iter_content(64 * 1024):
                        if chunk:
                            f.write(chunk)
            return str(target)
        except Exception as exc:
            logger.warning("tikwm download failed for %s: %s", target.name, exc)
            return None

    def download_by_tiktok_url(
        self,
        tiktok_url: str,
        dir_path: Optional[str] = None,
        hd: bool = True,
    ) -> Optional[str]:
        """Cross-mode helper: caller has only a TikTok URL (e.g. from the
        signed API), wants the watermark-free MP4 via tikwm. One extra API
        call to /api/?url=... fetches a fresh play URL, then streams it.
        """
        try:
            v = self.get_video(tiktok_url, hd=hd)
        except TikwmRateLimit:
            raise
        except Exception as exc:
            logger.warning("tikwm get_video failed for %s: %s", tiktok_url, exc)
            return None
        if not v:
            return None
        return self.download_video(v, dir_path=dir_path, hd=hd)

    # ---- paginated iterators (for interleaved download) ---------------
    def _iter_paginated(
        self,
        path: str,
        params: Dict[str, Any],
        list_key: str,
        page_size: int,
        total: int,
    ) -> Iterator[List[Dict]]:
        cursor = "0"
        seen_cursors = {"0"}
        yielded = 0
        while yielded < total:
            this_page = min(page_size, total - yielded)
            data = self._get(path, {**params, "count": this_page, "cursor": cursor})
            batch = data.get(list_key) or []
            if not batch:
                return
            yield batch[: total - yielded]
            yielded += len(batch[: total - yielded])
            if not (data.get("hasMore") or data.get("has_more")):
                return
            nxt = str(data.get("cursor", ""))
            if not nxt or nxt in seen_cursors:
                return
            seen_cursors.add(nxt)
            cursor = nxt
            time.sleep(random.uniform(1.0, 2.0))

    def iter_user_videos(self, username: str, count: int = 30) -> Iterator[List[Dict]]:
        return self._iter_paginated(
            "/user/posts", {"unique_id": username.lstrip("@")},
            list_key="videos", page_size=35, total=count,
        )

    def iter_hashtag_videos(self, name_or_id: str, count: int = 20) -> Iterator[List[Dict]]:
        if str(name_or_id).isdigit():
            challenge_id = str(name_or_id)
        else:
            info = self.get_hashtag_info(name_or_id)
            challenge_id = str(info.get("ch_id") or info.get("id") or "")
            if not challenge_id:
                raise RuntimeError(f"tikwm: could not resolve hashtag '{name_or_id}'")
        return self._iter_paginated(
            "/challenge/posts", {"challenge_id": challenge_id},
            list_key="videos", page_size=20, total=count,
        )

    def iter_sound_videos(self, music_id: str, count: int = 20) -> Iterator[List[Dict]]:
        music_id = extract_sound_id(music_id) or str(music_id)
        return self._iter_paginated(
            "/music/posts", {"music_id": str(music_id)},
            list_key="videos", page_size=30, total=count,
        )
