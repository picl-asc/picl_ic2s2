"""Typed exceptions for pyktok_2026.

These let downstream code distinguish recoverable from terminal failures.
"""
from __future__ import annotations


class PyktokError(Exception):
    """Base for all pyktok_2026 errors."""


class AuthRequired(PyktokError):
    """Caller needs to be logged into TikTok and isn't."""


class Throttled(PyktokError):
    """TikTok returned an empty body or statusCode=100002 after retries."""


class TargetNotFound(PyktokError):
    """User/hashtag/video/sound/playlist doesn't exist or is fully restricted."""


class SigningFailed(PyktokError):
    """byted_acrawler.frontierSign never loaded or returned nothing."""


class SetupRequired(PyktokError):
    """Playwright's Chromium binary is missing — run `playwright install chromium`."""
