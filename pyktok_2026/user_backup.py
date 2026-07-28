"""yt-dlp powered fallback for user-video discovery.

When TikTok blocks ``/api/post/item_list/`` for an account (the in-browser UI
shows "Something went wrong" on the Videos tab), pyktok's ``get_user_videos``
returns 0 even though the user clearly has videos. yt-dlp uses a different
extraction path and often still works.

The pipeline here is two-stage so the resulting CSV stays in pyktok's native
format:

  1. **Discover** numeric video IDs via ``yt_dlp`` with ``extract_flat=True``.
  2. **Fetch** each video's full metadata via ``DOMEngine.save_tiktok_multi_urls``
     (the same code path ``save_tiktok`` uses for single videos).

The output CSV matches what every other pyktok save_* call produces, so
downstream tooling does not need to know that a different discovery method
was used.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple, Union

from ._logging import get_logger
from .targets import TIKTOK_BASE, normalize_username, user_page_url, video_url

logger = get_logger("user_backup")

CookiesFromBrowser = Union[str, Tuple[str, ...], None]


def _default_metadata_filename(username: str) -> str:
    from ._csv import default_endpoint_filename
    return default_endpoint_filename("user_videos", normalize_username(username))


def _normalize_cookies_from_browser(value: CookiesFromBrowser) -> Optional[Tuple]:
    """Coerce caller input to the tuple shape yt-dlp expects.

    Accepts ``"chrome"``, ``("chrome",)``, or ``("chrome", profile, keyring, container)``.
    Returns ``None`` if the caller didn't ask for browser cookies.
    """
    if not value:
        return None
    if isinstance(value, str):
        return (value,)
    return tuple(value)


def discover_user_video_ids(
    username: str,
    count: Optional[int] = None,
    *,
    cookies_from_browser: CookiesFromBrowser = None,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
    **ydl_overrides: Any,
) -> List[str]:
    """Return a list of video IDs for ``username`` via yt-dlp.

    Parameters
    ----------
    username : str
        TikTok handle (with or without leading '@').
    count : int | None
        Max IDs to return. ``None`` means "everything yt-dlp can see".
    cookies_from_browser : str | tuple | None
        Optional. When set, yt-dlp imports cookies from the named browser
        (e.g. ``"chrome"``) so age- or login-gated profiles work. Accepts
        either ``"chrome"`` or the full yt-dlp tuple
        ``(browser, profile, keyring, container)``. Default: do not import.
    date_start, date_end : str | None
        Optional ``"YYYYMMDD"`` bounds (inclusive). Either may be set
        independently. When **either** is provided, yt-dlp runs with
        ``extract_flat="in_playlist"`` so entries carry the timestamp it
        needs to filter on. With **both** unset, fast flat extraction is used.
    **ydl_overrides
        Passed through to ``yt_dlp.YoutubeDL`` options, overriding defaults.

    Returns
    -------
    list[str]
        Numeric video IDs in the order yt-dlp returned them (newest first).
        Empty list if discovery failed.
    """
    try:
        from yt_dlp import YoutubeDL
    except ImportError as exc:
        raise ImportError(
            "user_backup requires yt-dlp. Install it with: pip install yt-dlp"
        ) from exc

    username = normalize_username(username)
    opts: Dict[str, Any] = {
        "quiet":         True,
        "no_warnings":   True,
        "extract_flat":  True,
        "skip_download": True,
    }
    if count is not None and count > 0:
        opts["playlistend"] = int(count)

    cfb = _normalize_cookies_from_browser(cookies_from_browser)
    if cfb is not None:
        opts["cookiesfrombrowser"] = cfb
        logger.info("yt-dlp using cookies from browser %s", cfb[0])

    if date_start or date_end:
        # DateRange needs at least one bound; either side can be None.
        try:
            from yt_dlp.utils import DateRange
            opts["daterange"] = DateRange(start=date_start, end=date_end)
        except Exception as exc:
            logger.warning("Could not build yt-dlp DateRange (%s) — proceeding without filter", exc)
        # Flat extraction may strip timestamps yt-dlp needs to evaluate the
        # daterange against; downgrade to in_playlist so each entry keeps
        # its upload_date/timestamp.
        opts["extract_flat"] = "in_playlist"
        logger.info("yt-dlp daterange filter: start=%s end=%s", date_start, date_end)

    opts.update(ydl_overrides)

    url = user_page_url(username)
    try:
        with YoutubeDL(opts) as ydl:
            info = ydl.extract_info(url, download=False)
    except Exception as exc:
        logger.warning("yt-dlp extract failed for @%s: %s", username, exc)
        return []

    entries = (info or {}).get("entries") or []
    ids: List[str] = []
    for e in entries:
        if not isinstance(e, dict):
            continue
        vid = e.get("id") or ""
        if not vid:
            # Some yt-dlp versions return the URL instead of a raw ID;
            # fall back to extracting from the URL.
            from .targets import extract_video_id
            vid = extract_video_id(e.get("url") or "") or ""
        if vid:
            ids.append(str(vid))

    if count is not None and count > 0:
        ids = ids[: int(count)]

    logger.info("yt-dlp discovered %d video IDs for @%s", len(ids), username)
    return ids


def build_video_urls(username: str, video_ids: List[str]) -> List[str]:
    """Construct canonical TikTok video URLs from a username and list of IDs."""
    return [video_url(username, vid) for vid in video_ids if vid]


def save_user_videos_via_backup(
    session,
    dom_engine,
    username: str,
    count: Optional[int] = None,
    *,
    save_video: bool = False,
    metadata_fn: str = "",
    sleep: int = 0.5,
    dir_path: Optional[str] = None,
    expected_total: Optional[int] = None,
    cookies_from_browser: CookiesFromBrowser = None,
    date_start: Optional[str] = None,
    date_end: Optional[str] = None,
) -> Dict[str, Any]:
    """Discover + scrape one user's videos via the yt-dlp fallback.

    Parameters
    ----------
    session : BrowserSession
        Shared pyktok session (used implicitly via dom_engine).
    dom_engine : DOMEngine
        The shared DOM engine; we call ``save_tiktok_multi_urls`` on it.
    username : str
        TikTok handle.
    count : int | None
        Target number of videos. ``None`` means "as many as yt-dlp returns".
    save_video : bool
        Pass through to ``dom_engine.save_tiktok_multi_urls``; downloads MP4s.
    metadata_fn : str
        Output CSV. Defaults to ``save_user_videos_<username>.csv``.
    sleep : int
        Per-video sleep, passed through to ``save_tiktok_multi_urls``.
    dir_path : str | None
        Optional directory for downloaded media files.
    expected_total : int | None
        Caller's prior knowledge of the user's full videoCount, used only
        for the "discovered < expected" warning.
    cookies_from_browser : str | tuple | None
        Optional. Forwarded to yt-dlp so a logged-in session is used during
        ID discovery. See ``discover_user_video_ids`` for accepted shapes.
    date_start, date_end : str | None
        Optional ``"YYYYMMDD"`` bounds for filtering discovered videos by
        upload date. Either may be set independently. With both unset,
        discovery is unfiltered (the common case).

    Returns
    -------
    dict
        ``{"username", "discovered", "expected_total", "csv", "rows"}``.
        ``rows`` is None when the CSV cannot be read back.
    """
    import os
    import pandas as pd

    username = normalize_username(username)
    if not metadata_fn:
        metadata_fn = _default_metadata_filename(username)

    ids = discover_user_video_ids(
        username,
        count=count,
        cookies_from_browser=cookies_from_browser,
        date_start=date_start,
        date_end=date_end,
    )
    discovered = len(ids)

    if expected_total and discovered < expected_total:
        logger.warning(
            "yt-dlp returned %d/%d videos for @%s — proceeding with partial set",
            discovered, expected_total, username,
        )

    if not ids:
        logger.warning(
            "yt-dlp returned 0 videos for @%s — nothing to scrape", username,
        )
        return {
            "username":       username,
            "discovered":     0,
            "expected_total": expected_total,
            "csv":            metadata_fn,
            "rows":           0,
        }

    urls = build_video_urls(username, ids)

    # Hand off to the existing per-video scraper. It already handles
    # dedupe-against-existing-CSV via _csv.deduplicate.
    dom_engine.save_tiktok_multi_urls(
        urls,
        save_video=save_video,
        metadata_fn=metadata_fn,
        sleep=sleep,
        dir_path=dir_path,
    )

    rows: Optional[int] = None
    if os.path.exists(metadata_fn):
        try:
            rows = len(pd.read_csv(metadata_fn))
        except Exception as exc:
            logger.debug("Could not read back %s: %s", metadata_fn, exc)

    return {
        "username":       username,
        "discovered":     discovered,
        "expected_total": expected_total,
        "csv":            metadata_fn,
        "rows":           rows,
    }
