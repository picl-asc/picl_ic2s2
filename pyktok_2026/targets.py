"""Parsing helpers for TikTok URLs and identifiers."""
from __future__ import annotations

import re
from typing import Optional

TIKTOK_BASE = "https://www.tiktok.com"
URL_REGEX = r"(?<=\.com/)(.+?)(?=\?|$)"
# Matches both /video/<id> and /photo/<id> (TikTok photo/slideshow posts).
VIDEO_ID_REGEX = r"/(?:video|photo)/([0-9]+)"
# Playlist URL: .../@user/playlist/<Name>-<digits>  → capture the trailing run of digits.
PLAYLIST_ID_REGEX = r"/playlist/(?:[^/?#]*?-)?([0-9]{6,})"
# Music URL: .../music/<Name>-<digits>  → capture the trailing run of digits.
MUSIC_ID_REGEX = r"/music/(?:[^/?#]*?-)?([0-9]{6,})"


def normalize_username(username: str) -> str:
    return (username or "").strip().lstrip("@").strip()


def normalize_hashtag(hashtag: str) -> str:
    return (hashtag or "").strip().lstrip("#").strip()


def extract_video_id(url_or_id: str) -> Optional[str]:
    """Return the numeric video ID from a TikTok URL or pass through if it already is one."""
    s = str(url_or_id or "")
    m = re.search(VIDEO_ID_REGEX, s)
    if m:
        return m.group(1)
    if s.isdigit():
        return s
    return None


def extract_playlist_id(url_or_id: str) -> Optional[str]:
    """Numeric mix/playlist id from a TikTok playlist URL, or pass through an id.

    Accepts ``https://www.tiktok.com/@user/playlist/Name-7281443725770476321``
    or the bare ``7281443725770476321``.
    """
    s = str(url_or_id or "").split("?")[0].split("#")[0]
    m = re.search(PLAYLIST_ID_REGEX, s)
    if m:
        return m.group(1)
    return s if s.isdigit() else None


def extract_sound_id(url_or_id: str) -> Optional[str]:
    """Numeric music id from a TikTok music URL, or pass through an id.

    Accepts ``https://www.tiktok.com/music/Song-Name-7016547803243022337``
    or the bare ``7016547803243022337``.
    """
    s = str(url_or_id or "").split("?")[0].split("#")[0]
    m = re.search(MUSIC_ID_REGEX, s)
    if m:
        return m.group(1)
    return s if s.isdigit() else None


def video_url(username: str, video_id: str) -> str:
    return f"{TIKTOK_BASE}/@{normalize_username(username)}/video/{video_id}"


def user_page_url(username: str) -> str:
    return f"{TIKTOK_BASE}/@{normalize_username(username)}"


def hashtag_page_url(hashtag: str) -> str:
    return f"{TIKTOK_BASE}/tag/{normalize_hashtag(hashtag)}"
