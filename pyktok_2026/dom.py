"""DOMEngine — browser-based TikTok scraping.

Renders profile / hashtag / video pages in Playwright Chromium and reads them.
The "single video" path extracts __UNIVERSAL_DATA_FOR_REHYDRATION__ — the
canonical source. Multi-page scrolling falls back to clicking through tiles
when the page-JSON itemList runs out.
"""
from __future__ import annotations

import base64
import json
import os
import random
import re
import time
from typing import Any, Dict, List, Optional

import pandas as pd
import requests

from ._browser import BrowserSession
from ._csv import (
    HEADERS,
    deduplicate,
    default_endpoint_filename,
    default_multi_page_metadata_filename,
    extract_page_json,
    extract_video_struct,
    generate_data_row,
    media_filename,
    safe,
)
from ._logging import get_logger
from .targets import TIKTOK_BASE, URL_REGEX, VIDEO_ID_REGEX, normalize_hashtag, normalize_username

logger = get_logger("dom")


class DOMEngine:
    """Browser-based TikTok scraper. Uses a shared BrowserSession."""

    def __init__(self, session: BrowserSession):
        self.session = session

    # ---- single-video paths -------------------------------------------
    def get_tiktok_json(self, video_url: str) -> Optional[Dict[str, Any]]:
        self.session.go(video_url, wait=2.0)
        tt_json = extract_page_json(self.session.page_source())
        if tt_json is None:
            logger.warning("No page JSON found at %s", video_url)
        return tt_json

    def save_tiktok(
        self,
        content_url: str,
        save_video: bool = False,
        metadata_fn: str = "",
        dir_path: Optional[str] = None,
    ):
        if not save_video and not metadata_fn:
            logger.info("Nothing to do — both save_video and metadata_fn are empty.")
            return None
        tt_json = self.get_tiktok_json(content_url)
        if tt_json is None:
            return None
        from .targets import extract_video_id as _evid
        requested_id = _evid(content_url)
        video_id, item = extract_video_struct(tt_json, prefer_id=requested_id)
        if not video_id or not item:
            logger.error("No video struct in page JSON for %s", content_url)
            return None
        if requested_id and str(video_id) != str(requested_id):
            logger.warning(
                "Page for %s served item %s, not the requested %s — TikTok often does this for "
                "/photo/ posts or restricted/redirected URLs. Saving what was served; the metadata "
                "row may not be the post you expected.",
                content_url, video_id, requested_id,
            )

        content_paths: List[str] = []

        if save_video:
            author = safe(item, "author", "uniqueId", default="")
            if "imagePost" in item:
                for i, slide in enumerate(item["imagePost"].get("images", []), start=1):
                    img_url = slide.get("imageURL", {}).get("urlList", [""])[0]
                    if img_url:
                        content_paths.append(
                            self._download_file(img_url, media_filename(author, video_id, slide=i), dir_path)
                        )
            else:
                dl_url = (safe(item, "video", "downloadAddr", default="")
                          or safe(item, "video", "playAddr", default=""))
                if dl_url:
                    content_paths.append(
                        self._download_file(dl_url, media_filename(author, video_id), dir_path)
                    )
                else:
                    logger.warning("No download URL found for %s", content_url)

        if metadata_fn:
            row = generate_data_row(item, engine="dom")
            combined = deduplicate(metadata_fn, row, key="video_id")
            combined.to_csv(metadata_fn, index=False)
            logger.info("Metadata → %s", metadata_fn)

        return content_paths, metadata_fn

    def save_tiktok_multi_urls(
        self,
        video_urls,
        save_video: bool = False,
        metadata_fn: str = "",
        sleep: int = 4,
        dir_path: Optional[str] = None,
    ):
        if isinstance(video_urls, str):
            with open(video_urls) as f:
                video_urls = [line.strip() for line in f if line.strip()]
        from .targets import extract_video_id
        written = []
        for url in video_urls:
            # No metadata_fn  -> a SEPARATE auto-named CSV per URL (video_<id>_<ts>.csv).
            # A given metadata_fn -> append every row into that one combined file.
            fn = metadata_fn or default_endpoint_filename("video", extract_video_id(url) or "unknown")
            self.save_tiktok(url, save_video=save_video, metadata_fn=fn, dir_path=dir_path)
            written.append(fn)
            time.sleep(random.uniform(0.5, max(0.5, sleep)))
        logger.info("Processed %d URLs", len(video_urls))
        return list(dict.fromkeys(written))  # unique CSV paths, order-preserving

    # ---- multi-page scrolling -----------------------------------------
    def save_tiktok_multi_page_scroll(
        self,
        entity: str,
        entity_type: str = "user",
        video_ct: int = 30,
        save_video: bool = False,
        metadata_fn: str = "",
        sleep: int = 4,
    ):
        if entity_type == "user":
            entity = normalize_username(entity)
            page_url = f"{TIKTOK_BASE}/@{entity}"
        elif entity_type == "hashtag":
            entity = normalize_hashtag(entity)
            page_url = f"{TIKTOK_BASE}/tag/{entity}"
        elif entity_type == "video_related":
            page_url = entity
        else:
            raise ValueError('entity_type must be "user", "hashtag", or "video_related"')

        if not metadata_fn:
            metadata_fn = default_multi_page_metadata_filename(entity, entity_type)
            logger.info("Default metadata filename: %s", metadata_fn)

        page = self.session.page
        self.session.go(page_url, wait=3.0)

        seen: set = set()
        if metadata_fn and os.path.exists(metadata_fn):
            try:
                existing = pd.read_csv(metadata_fn, keep_default_na=False)
                if "video_id" in existing.columns:
                    seen.update(str(v) for v in existing["video_id"].tolist())
            except Exception:
                pass
        saved = len(seen)

        pending_urls: list = []

        # Fast path: page-JSON itemList
        try:
            tt_json = extract_page_json(self.session.page_source())
            if tt_json and "__DEFAULT_SCOPE__" in tt_json:
                scope = tt_json["__DEFAULT_SCOPE__"]
                if entity_type == "user":
                    item_list = scope.get("webapp.user-detail", {}).get("itemList", [])
                    for vid in item_list:
                        if str(vid) in seen:
                            continue
                        seen.add(str(vid))
                        pending_urls.append(f"{TIKTOK_BASE}/@{entity}/video/{vid}")
                elif entity_type == "hashtag":
                    for vid_id, item in tt_json.get("ItemModule", {}).items():
                        if vid_id in seen:
                            continue
                        author = safe(item, "author", "uniqueId", default="")
                        if author:
                            seen.add(vid_id)
                            pending_urls.append(f"{TIKTOK_BASE}/@{author}/video/{vid_id}")
            logger.info("JSON fast path extracted %d candidate URLs", len(pending_urls))
        except Exception as exc:
            logger.debug("JSON fast path failed: %s", exc)

        # Tunables — fix with the test on May 16, 2026 for `mouse.wheel` scrolling:
        #   user    : ~12-16 new tiles per scroll until exhaustion; rarely stalls
        #             mid-stream. max_no_new=15 absorbs end-of-feed quirks.
        #   hashtag : similar to user, but TikTok caps tag feeds at a few
        #             hundred items.
        #   related : ~15-16 new tiles per *burst*, separated by 5-8 silent
        #             scrolls while TikTok cooks the next batch. Need
        #             max_no_new>=10 to not bail out mid-burst.
        if entity_type == "user":
            max_no_new, max_scrolls = 15, max(200, video_ct)
        elif entity_type == "hashtag":
            max_no_new, max_scrolls = 12, max(100, video_ct)
        else:  # video_related
            max_no_new, max_scrolls = 12, max(200, video_ct * 2)

        # Pre-position the cursor in the page so wheel events have a target.
        try:
            page.mouse.move(640, 400)
        except Exception:
            pass

        target = video_ct - saved
        no_new = 0
        for scroll_n in range(max_scrolls):
            if len(pending_urls) >= target:
                break
            try:
                hrefs = page.evaluate(
                    """() => Array.from(document.querySelectorAll('a[href*="/video/"]'))
                                  .map(a => a.href)"""
                )
            except Exception as exc:
                logger.debug("link collection failed: %s", exc)
                hrefs = []

            found_new = 0
            for href in hrefs or []:
                if "/video/" not in href:
                    continue
                m = re.search(VIDEO_ID_REGEX, href)
                vid_id = m.group(1) if m else href
                if vid_id in seen:
                    continue
                seen.add(vid_id)
                pending_urls.append(href)
                found_new += 1
                if len(pending_urls) >= target:
                    break

            if found_new == 0:
                no_new += 1
                if no_new >= max_no_new:
                    logger.info("No new videos after %d quiet scrolls — collected %d",
                                max_no_new, len(pending_urls))
                    break
            else:
                no_new = 0
                logger.debug("scroll %d: +%d new tiles (total %d/%d)",
                             scroll_n + 1, found_new, len(pending_urls), video_ct)

            # How many video tiles are attached right now — so we can wait for
            # the lazy-loader to actually deliver MORE after we scroll, instead
            # of guessing with a fixed sleep.
            try:
                count_before = page.evaluate(
                    """() => document.querySelectorAll('a[href*="/video/"]').length"""
                )
            except Exception:
                count_before = len(seen)

            # Drive the loader three ways: pull the LAST tile into view (handles
            # TikTok's virtualized grid / custom scroll container), jump to the
            # document bottom, and fire a real wheel event (the loader listens
            # on IntersectionObserver, which scrollTo alone doesn't trigger).
            try:
                page.evaluate(
                    """() => {
                        const els = document.querySelectorAll('a[href*="/video/"]');
                        if (els.length) els[els.length - 1].scrollIntoView({block: 'end'});
                        window.scrollTo(0, document.documentElement.scrollHeight);
                    }"""
                )
            except Exception:
                pass
            try:
                page.mouse.wheel(0, 3000)
            except Exception:
                try:
                    page.evaluate("() => window.scrollBy(0, 3000)")
                except Exception:
                    pass

            # Wait (up to ~5s) for new tiles to attach rather than a blind sleep.
            # If none arrive, this returns fast and the no_new counter advances.
            try:
                page.wait_for_function(
                    """(n) => document.querySelectorAll('a[href*="/video/"]').length > n""",
                    arg=count_before, timeout=5000,
                )
            except Exception:
                pass
            time.sleep(random.uniform(0.5, 1.0))

        if not pending_urls and saved == 0:
            hint = (
                "DOM mode found no videos on this page. Likely causes:\n"
                "  • TikTok is serving a logged-out view (your persistent context has no auth cookies).\n"
                "    Re-run with `login_if_needed=True` or call pyk.login() to refresh.\n"
                "  • Headless Chromium can't trigger the async JS that lazy-loads video tiles.\n"
                "    Try `pyk.specify_browser('chrome', headless=False)`.\n"
                "  • Switch to API mode (`use_api=True`) — it doesn't need login."
            )
            raise RuntimeError(f"DOM scroll collected 0 video URLs for '{entity}' ({entity_type}).\n{hint}")

        logger.info("Collected %d video URLs via scroll; saving metadata for each…", len(pending_urls))

        for href in pending_urls:
            if saved >= video_ct:
                break
            self.save_tiktok(href, save_video=save_video, metadata_fn=metadata_fn)
            saved += 1
            logger.info("Saved %d/%d — %s", saved, video_ct, href)
            time.sleep(random.uniform(0.3, max(0.3, sleep)))

        logger.info("scroll done: saved=%d/%d → %s", saved, video_ct, metadata_fn)
        return metadata_fn

    # ---- DOM-mode comments (unsigned in-browser fetch) ----------------
    def save_tiktok_comments_dom(
        self,
        video_url: str,
        filename: str = "",
        comment_count: int = 30,
        save_comments: bool = True,
        return_comments: bool = True,
    ):
        m = re.search(VIDEO_ID_REGEX, str(video_url))
        if not m:
            logger.warning("Could not extract video ID from %s", video_url)
            return pd.DataFrame() if return_comments else None
        aweme_id = m.group(1)

        try:
            current = self.session.page.url or ""
        except Exception:
            current = ""
        if "tiktok.com" not in current:
            self.session.go(TIKTOK_BASE, wait=2.0)

        comments: List[Dict] = []
        cursor = 0
        while len(comments) < comment_count:
            api_url = (
                f"/api/comment/list/?aid=1988&app_language=en&app_name=tiktok_web"
                f"&aweme_id={aweme_id}&count=20&cursor={cursor}"
            )
            try:
                raw = self.session.page.evaluate(
                    """async (u) => {
                        try {
                            const r = await fetch(u, {
                                credentials: 'include',
                                headers: { 'Referer': 'https://www.tiktok.com/' }
                            });
                            return await r.text();
                        } catch (err) {
                            return JSON.stringify({ __error: String(err) });
                        }
                    }""",
                    api_url,
                )
            except Exception as exc:
                logger.warning("Browser comment-fetch failed: %s", exc)
                break
            try:
                data = json.loads(raw)
            except (json.JSONDecodeError, TypeError):
                break
            if isinstance(data, dict) and "__error" in data:
                logger.warning("Comment API error: %s", data["__error"])
                break
            for c in (data.get("comments") or []):
                comments.append(c)
                if len(comments) >= comment_count:
                    break
            if not data.get("has_more", False):
                break
            cursor = data.get("cursor", 0)

        if not filename:
            filename = f"comments_{aweme_id}.csv"

        COMMENT_COLS = ["cid", "aweme_id", "text", "digg_count",
                        "reply_comment_total", "create_time",
                        "user_unique_id", "user_nickname", "user_uid", "user",
                        "data_collection_engine"]
        rows = []
        for c in comments:
            user = c.get("user", {}) if isinstance(c.get("user"), dict) else {}
            rows.append({
                "cid":                 c.get("cid", ""),
                "aweme_id":            aweme_id,
                "text":                c.get("text", ""),
                "digg_count":          c.get("digg_count", 0),
                "reply_comment_total": c.get("reply_comment_total", 0),
                "create_time":         c.get("create_time", 0),
                "user_unique_id":      user.get("unique_id", ""),
                "user_nickname":       user.get("nickname", ""),
                "user_uid":            user.get("uid", ""),
                "user":                json.dumps(user, ensure_ascii=False) if user else "",
                "data_collection_engine": "dom",
            })
        df = pd.DataFrame(rows, columns=COMMENT_COLS)

        if save_comments and not df.empty:
            combined = deduplicate(filename, df, key="cid")
            combined.to_csv(filename, index=False)
            logger.info("DOM comments → %s (%d rows)", filename, len(combined))
        elif save_comments:
            df.to_csv(filename, index=False)
            logger.warning("No comments found; wrote header-only CSV → %s", filename)

        return df if return_comments else None

    # ---- file download helper -----------------------------------------
    def _download_file(self, url: str, filename: str, dir_path: Optional[str] = None) -> str:
        path = os.path.join(dir_path, filename) if dir_path else filename
        if dir_path:
            os.makedirs(dir_path, exist_ok=True)   # create the target folder if missing
        if os.path.exists(path):
            logger.info("Already exists, skipping: %s", path)
            return path

        hdrs = {**HEADERS, "referer": f"{TIKTOK_BASE}/"}

        # Attempt 1: plain requests with cookies pulled from the Playwright context
        try:
            cookies = {}
            try:
                for c in self.session.context.cookies("https://www.tiktok.com"):
                    cookies[c["name"]] = c["value"]
            except Exception:
                pass
            with requests.get(url, headers=hdrs, cookies=cookies, stream=True, timeout=60) as r:
                r.raise_for_status()
                with open(path, "wb") as f:
                    for chunk in r.iter_content(chunk_size=8192):
                        if chunk:
                            f.write(chunk)
            logger.info("Downloaded → %s", path)
            return path
        except Exception as exc:
            logger.warning("Plain requests download failed (%s) — trying browser fetch…", exc)

        # Attempt 2: fetch inside the page so TikTok's CDN accepts the request
        page = self.session.page
        info = page.evaluate(
            """async (u) => {
                try {
                    const r = await fetch(u, { credentials: 'include' });
                    if (!r.ok) return { error: 'HTTP ' + r.status };
                    const buf = await r.arrayBuffer();
                    window._pyk_blob = buf;
                    return { size: buf.byteLength };
                } catch (err) { return { error: String(err) }; }
            }""",
            url,
        )
        if isinstance(info, dict) and "error" in info:
            raise RuntimeError(f"Browser fetch failed: {info['error']}")
        total = info.get("size", 0) if isinstance(info, dict) else 0
        logger.info("Browser fetched %d bytes — reading in chunks…", total)

        CHUNK = 65536
        with open(path, "wb") as f:
            offset = 0
            while offset < total:
                length = min(CHUNK, total - offset)
                b64 = page.evaluate(
                    """([offset, length]) => {
                        const slice = new Uint8Array(window._pyk_blob, offset, length);
                        let bin = '';
                        for (let i = 0; i < slice.length; i += 1024) {
                            bin += String.fromCharCode.apply(null, slice.subarray(i, i + 1024));
                        }
                        return btoa(bin);
                    }""",
                    [offset, length],
                )
                f.write(base64.b64decode(b64))
                offset += length
        page.evaluate("() => { delete window._pyk_blob; }")
        logger.info("Downloaded (via browser) → %s", path)
        return path
