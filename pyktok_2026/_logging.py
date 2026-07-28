"""Quiet-by-default logging. Call set_verbosity('info') to opt into chatty logs."""
from __future__ import annotations

import logging
import sys

_ROOT = "pyktok_2026"
_HANDLER: logging.Handler | None = None


def _get_handler() -> logging.Handler:
    global _HANDLER
    if _HANDLER is None:
        _HANDLER = logging.StreamHandler(sys.stderr)
        _HANDLER.setFormatter(logging.Formatter(
            "%(asctime)s [%(levelname)s] %(name)s: %(message)s",
            datefmt="%H:%M:%S",
        ))
    return _HANDLER


def get_logger(suffix: str = "") -> logging.Logger:
    name = f"{_ROOT}.{suffix}" if suffix else _ROOT
    logger = logging.getLogger(name)
    if not logger.handlers and logger is logging.getLogger(_ROOT):
        logger.addHandler(_get_handler())
        logger.setLevel(logging.WARNING)
        logger.propagate = False
    return logger


def set_verbosity(level: str | int) -> None:
    """Set verbosity. Accepts 'debug', 'info', 'warn'/'warning', 'error', or a numeric level."""
    if isinstance(level, str):
        level = {
            "debug": logging.DEBUG,
            "info": logging.INFO,
            "warn": logging.WARNING,
            "warning": logging.WARNING,
            "error": logging.ERROR,
            "quiet": logging.ERROR,
        }.get(level.lower(), logging.WARNING)
    logging.getLogger(_ROOT).setLevel(level)
