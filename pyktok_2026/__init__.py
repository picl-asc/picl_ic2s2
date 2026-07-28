"""pyktok_2026 — TikTok metadata collection for academic research.

The next generation of pyktok. See README.md for the full public API.
"""
from __future__ import annotations

__version__ = "2.0"

from .exceptions import (
    AuthRequired,
    PyktokError,
    SetupRequired,
    SigningFailed,
    TargetNotFound,
    Throttled,
)
from .facade import (
    close,
    collect,
    download_video,
    get_comment_replies,
    get_hashtag_info,
    get_hashtag_videos,
    get_playlist_info,
    get_playlist_videos,
    get_related_videos,
    get_sound_info,
    get_sound_videos,
    get_tiktok_comments,
    get_tiktok_json,
    get_trending_videos,
    get_user_info,
    get_user_videos,
    get_video_comments,
    healthcheck,
    login,
    login_status,
    login_with_cookies,
    login_with_state,
    reset,
    save_tiktok,
    save_tiktok_comments,
    save_tiktok_multi_page,
    save_tiktok_multi_urls,
    save_user,
    search_hashtags,
    search_keyword,
    search_users,
    search_videos,
    set_verbosity,
    setup,
    specify_browser,
)
from .tikwm import (
    TikwmEngine,
    TikwmRateLimit,
    tikwm_quota_remaining,
    tikwm_quota_reset_seconds,
)

__all__ = [
    "__version__",
    # setup
    "specify_browser", "close", "reset", "login", "set_verbosity", "setup", "healthcheck",
    # login (manual / no-QR + diagnostics)
    "login_with_cookies", "login_with_state", "login_status",
    # single video
    "get_tiktok_json", "save_tiktok", "save_tiktok_multi_urls", "download_video",
    # multi-page
    "save_tiktok_multi_page",
    # resumable batch helper
    "collect",
    # user backup (yt-dlp + DOM)
    "save_user",
    # comments
    "save_tiktok_comments", "get_tiktok_comments", "get_video_comments",
    "get_comment_replies",
    # hidden API convenience
    "get_user_info", "get_user_videos", "get_hashtag_videos",
    "get_related_videos", "get_trending_videos",
    "search_users", "search_keyword", "search_videos", "search_hashtags",
    "get_hashtag_info",
    "get_sound_info", "get_sound_videos",
    "get_playlist_info", "get_playlist_videos",
    # tikwm engine + rate-limit exception (for catch-and-resume loops)
    "TikwmEngine", "TikwmRateLimit", "tikwm_quota_remaining", "tikwm_quota_reset_seconds",
    # exceptions
    "PyktokError", "AuthRequired", "Throttled", "TargetNotFound",
    "SigningFailed", "SetupRequired",
]
