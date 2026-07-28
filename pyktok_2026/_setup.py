"""One-time setup helper. Installs the Playwright Chromium binary if missing."""
from __future__ import annotations

import subprocess
import sys

from ._logging import get_logger
from .exceptions import SetupRequired

logger = get_logger("setup")


def chromium_installed() -> bool:
    """Return True if Playwright can find a Chromium binary."""
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            path = pw.chromium.executable_path
            import os
            return bool(path and os.path.exists(path))
    except Exception:
        return False


def install_chromium() -> None:
    """Run `playwright install chromium` in-process."""
    logger.warning("Installing Playwright Chromium (one-time)...")
    cmd = [sys.executable, "-m", "playwright", "install", "chromium"]
    rc = subprocess.call(cmd)
    if rc != 0:
        raise SetupRequired(
            f"`playwright install chromium` exited with code {rc}. "
            "Re-run manually to see the full output."
        )


def setup() -> None:
    """Ensure all required runtime dependencies are present."""
    if not chromium_installed():
        install_chromium()
    logger.warning("pyktok_2026 setup OK.")
