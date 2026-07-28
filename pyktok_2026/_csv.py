"""Shared row builders, schemas, and CSV merge/dedup helpers.

These are the canonical schemas — both the API engine and the DOM engine produce
rows through this module so downstream code sees identical columns regardless of
which mode collected the data.
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from typing import Any, Dict, Optional

import pandas as pd

from .targets import TIKTOK_BASE, URL_REGEX, extract_video_id

# ---------------------------------------------------------------------------
# Video metadata schema (lifted verbatim from legacy DOM.py)
# ---------------------------------------------------------------------------
DATA_COLUMNS = [
    "video_id", "video_timestamp", "video_duration", "video_locationcreated",
    "video_diggcount", "video_sharecount", "video_commentcount", "video_playcount",
    "video_description", "video_is_ad", "video_stickers",
    "author_username", "author_name",
    "author_followercount", "author_followingcount", "author_heartcount",
    "author_videocount", "author_diggcount", "author_verified",
    "poi_name", "poi_address", "poi_city",
    "data_collection_engine",   # which engine produced this row: api | dom | tikwm
]

# Comment CSV schema (extends pyktok's legacy fields with helpful user-info columns)
COMMENT_EXPORT_COLUMNS = [
    "allow_download_photo", "author_pin", "aweme_id", "cid", "collect_stat",
    "comment_language", "comment_post_item_ids", "create_time", "digg_count",
    "image_list", "is_author_digged", "is_comment_translatable",
    "is_high_purchase_intent", "label_list", "no_show", "reply_comment",
    "reply_comment_total", "reply_id", "reply_to_reply_id", "share_info",
    "sort_extra_score", "sort_tags", "status", "stick_position", "text",
    "text_extra", "trans_btn_style", "user", "user_buried", "user_digged",
    "user_unique_id", "user_nickname", "user_uid",
    "data_collection_engine",   # which engine produced this row: api | dom | tikwm
]

# Default HTTP headers for direct downloads
HEADERS = {
    "Accept-Encoding": "gzip, deflate, sdch",
    "Accept-Language": "en-US,en;q=0.8",
    "Upgrade-Insecure-Requests": "1",
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
    ),
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
    "Cache-Control": "max-age=0",
    "Connection": "keep-alive",
}


def safe(obj, *keys, default=""):
    """Walk nested dicts; return default if any key is missing."""
    for k in keys:
        if isinstance(obj, dict):
            obj = obj.get(k)
        else:
            return default
    return obj if obj is not None else default


def sanitize_filename_part(s: str) -> str:
    s = (s or "").strip()
    for ch in '<>:"\\|?*':
        s = s.replace(ch, "")
    return s.replace("/", "_").replace(" ", "_")


def run_stamp() -> str:
    """A filename-safe run timestamp, e.g. 20260626T143052 (%Y%m%dT%H%M%S).

    Generated once per call so every run produces a distinct file and nothing
    is ever silently overwritten (naming Scheme C).
    """
    return datetime.now().strftime("%Y%m%dT%H%M%S")


def media_filename(author: str, video_id: str, slide: Optional[int] = None) -> str:
    """Media (MP4 / image) filename: ``<author>_<vid>.mp4`` — no timestamp,
    because a video file is content-addressed by its immutable id (re-downloads
    are identical bytes). Image-post slides get ``_slide_<n>.jpeg``.
    """
    author = sanitize_filename_part(author or "")
    base = f"{author}_{video_id}" if author else str(video_id)
    if slide is not None:
        return f"{base}_slide_{slide}.jpeg"
    return f"{base}.mp4"


def default_multi_page_metadata_filename(entity: str, entity_type: str) -> str:
    if entity_type == "video_related":
        ident = extract_video_id(entity) or sanitize_filename_part(str(entity))
        stem = f"related_{ident}"
    else:
        ident = sanitize_filename_part(str(entity))
        stem = {
            "user":    f"user_{ident}",
            "hashtag": f"hashtag_{ident}",
            "sound":   f"sound_{ident}",
        }.get(entity_type, f"{entity_type}_{ident}")
    return f"{stem}_{run_stamp()}.csv"


def default_endpoint_filename(endpoint: str, identifier: str) -> str:
    ident = sanitize_filename_part(str(identifier))
    stem = {
        "user_videos":     f"user_{ident}",
        "user_info":       f"userinfo_{ident}",
        "hashtag_videos":  f"hashtag_{ident}",
        "hashtag_info":    f"hashtaginfo_{ident}",
        "video_comments":  f"comments_{ident}",
        "comment_replies": f"replies_{ident}",
        "related_videos":  f"related_{ident}",
        "trending":        f"trending_{ident}" if ident else "trending",
        "search_videos":   f"searchvideos_{ident}",
        "search_keyword":  f"searchusers_{ident}",
        "search_hashtags": f"searchtags_{ident}",
        "sound_videos":    f"sound_{ident}",
        "sound_info":      f"soundinfo_{ident}",
        "playlist_videos": f"playlist_{ident}",
        "playlist_info":   f"playlistinfo_{ident}",
        "video":           f"video_{ident}",
    }.get(endpoint, f"{endpoint}_{ident}")
    return f"{stem}_{run_stamp()}.csv"


def generate_data_row(video_obj: Dict[str, Any], engine: str = "") -> pd.DataFrame:
    """One TikTok itemStruct → one-row DataFrame matching DATA_COLUMNS.

    ``engine`` (``'api'`` | ``'dom'`` | ``'tikwm'``) is recorded in the
    ``data_collection_engine`` column so each row carries its provenance.
    """
    try:
        ts = datetime.fromtimestamp(int(video_obj["createTime"])).isoformat()
    except Exception:
        ts = ""
    try:
        stickers = ";".join(
            t for s in video_obj.get("stickersOnItem", []) for t in s.get("stickerText", [])
        )
    except Exception:
        stickers = ""
    row = {
        "video_id":              video_obj.get("id", ""),
        "video_timestamp":       ts,
        "video_duration":        safe(video_obj, "video", "duration", default=""),
        "video_locationcreated": video_obj.get("locationCreated", ""),
        "video_diggcount":       safe(video_obj, "stats", "diggCount", default=""),
        "video_sharecount":      safe(video_obj, "stats", "shareCount", default=""),
        "video_commentcount":    safe(video_obj, "stats", "commentCount", default=""),
        "video_playcount":       safe(video_obj, "stats", "playCount", default=""),
        "video_description":     video_obj.get("desc", ""),
        "video_is_ad":           video_obj.get("isAd", False),
        "video_stickers":        stickers,
        "author_username":       safe(video_obj, "author", "uniqueId", default=""),
        "author_name":           safe(video_obj, "author", "nickname", default=""),
        "author_followercount":  safe(video_obj, "authorStats", "followerCount", default=""),
        "author_followingcount": safe(video_obj, "authorStats", "followingCount", default=""),
        "author_heartcount":     safe(video_obj, "authorStats", "heartCount", default=""),
        "author_videocount":     safe(video_obj, "authorStats", "videoCount", default=""),
        "author_diggcount":      safe(video_obj, "authorStats", "diggCount", default=""),
        "author_verified":       safe(video_obj, "author", "verified", default=False),
        "poi_name":              safe(video_obj, "poi", "name", default=""),
        "poi_address":           safe(video_obj, "poi", "address", default=""),
        "poi_city":              safe(video_obj, "poi", "city", default=""),
        "data_collection_engine": engine,
    }
    return pd.DataFrame([row], columns=DATA_COLUMNS)


def comment_export_row(comment: Dict[str, Any], aweme_id: str, engine: str = "") -> Dict[str, Any]:
    user = comment.get("user", {}) if isinstance(comment.get("user"), dict) else {}
    row = {col: comment.get(col, "") for col in COMMENT_EXPORT_COLUMNS}
    row["aweme_id"] = aweme_id
    row["user_unique_id"] = user.get("unique_id", "")
    row["user_nickname"] = user.get("nickname", "")
    row["user_uid"] = user.get("uid", "")
    row["user"] = json.dumps(user, ensure_ascii=False) if user else ""
    row["data_collection_engine"] = engine
    return row


def deduplicate(existing_path: str, new_df: pd.DataFrame, key: str = "video_id") -> pd.DataFrame:
    """Merge new_df with the existing CSV at existing_path and drop duplicates on key."""
    if os.path.exists(existing_path):
        try:
            old = pd.read_csv(existing_path, keep_default_na=False)
            new_df = pd.concat([old, new_df], ignore_index=True)
        except Exception:
            pass
    if key in new_df.columns:
        new_df[key] = new_df[key].astype(str)
        return new_df.drop_duplicates(subset=[key])
    return new_df


def extract_page_json(html: str) -> Optional[Dict[str, Any]]:
    """Pull __UNIVERSAL_DATA_FOR_REHYDRATION__ (current) or SIGI_STATE (legacy)."""
    from bs4 import BeautifulSoup
    soup = BeautifulSoup(html, "html.parser")
    for script_id in ("__UNIVERSAL_DATA_FOR_REHYDRATION__", "SIGI_STATE"):
        tag = soup.find("script", {"id": script_id})
        if tag and tag.string:
            try:
                return json.loads(tag.string)
            except json.JSONDecodeError:
                continue
    return None


def extract_video_struct(tt_json: Dict[str, Any], prefer_id: Optional[str] = None):
    """Return (video_id, itemStruct) from either current or legacy page JSON.

    ``prefer_id`` (the id from the requested URL) disambiguates the legacy
    ``ItemModule`` map, which can hold several items — without it we'd grab the
    first, which on a /photo/ or restricted page is often a *recommended* video,
    not the one asked for.
    """
    prefer = str(prefer_id) if prefer_id else None
    if "__DEFAULT_SCOPE__" in tt_json:
        vd = tt_json["__DEFAULT_SCOPE__"].get("webapp.video-detail", {})
        item_info = vd.get("itemInfo")
        if item_info is None:
            return None, None
        struct = item_info.get("itemStruct", {})
        return struct.get("id"), struct
    if "ItemModule" in tt_json and tt_json["ItemModule"]:
        im = tt_json["ItemModule"]
        if prefer and prefer in im:
            return prefer, im[prefer]
        vid_id = next(iter(im))
        return vid_id, im[vid_id]
    return None, None
