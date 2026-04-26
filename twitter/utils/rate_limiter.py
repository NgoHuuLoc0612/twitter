"""
RateLimiter — per-endpoint Twitter API v2 rate-limit budget tracker.

Twitter returns three headers on every response:
  x-rate-limit-limit     : total requests allowed per window
  x-rate-limit-remaining : requests left in current window
  x-rate-limit-reset     : Unix epoch when the window resets

This module:
  1. Parses those headers and persists them to SQLite.
  2. Before each request, checks the remaining budget and sleeps
     until the reset window if the budget is exhausted.
  3. Adds a configurable per-call minimum delay to stay well inside limits.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Dict, Optional

if TYPE_CHECKING:
    from twitter.cache.db import CacheDB

log = logging.getLogger(__name__)

# Twitter v2 rate-limit headers
_H_LIMIT = "x-rate-limit-limit"
_H_REMAINING = "x-rate-limit-remaining"
_H_RESET = "x-rate-limit-reset"

# Safety margin: treat budget as exhausted when this many remain
_SAFETY_FLOOR = 2

# Minimum seconds between consecutive calls to the same endpoint
_MIN_DELAY: Dict[str, float] = {
    "/users/{id}/following": 1.0,   # follow: 15 req / 15 min
    "/tweets": 0.5,
    "default": 0.2,
}


def _endpoint_key(url: str) -> str:
    """Normalise a full URL or path to a stable dict key."""
    # Strip query string and base URL, keep the path skeleton
    path = url.split("?")[0]
    if "api.twitter.com" in path:
        path = "/" + "/".join(path.split("/")[3:])
    return path


class RateLimiter:
    """Wraps CacheDB to enforce Twitter rate-limit windows client-side."""

    def __init__(self, db: "CacheDB"):
        self._db = db
        self._last_call: Dict[str, float] = {}

    # ------------------------------------------------------------------
    # Called BEFORE each request
    # ------------------------------------------------------------------

    def wait_if_needed(self, endpoint: str) -> None:
        key = _endpoint_key(endpoint)

        # --- Minimum inter-request delay ---
        min_delay = _MIN_DELAY.get(key, _MIN_DELAY["default"])
        last = self._last_call.get(key, 0.0)
        elapsed = time.time() - last
        if elapsed < min_delay:
            sleep_for = min_delay - elapsed
            log.debug("Rate-limit delay %.2fs for %s", sleep_for, key)
            time.sleep(sleep_for)

        # --- Check persisted rate-limit window ---
        row = self._db.get_rate_limit(key)
        if row and row["remaining"] is not None:
            remaining = row["remaining"]
            reset_at = row["reset_at"] or 0
            if remaining <= _SAFETY_FLOOR and reset_at > time.time():
                sleep_for = reset_at - time.time() + 1.0
                log.warning(
                    "Rate limit budget exhausted for %s — sleeping %.1fs until reset",
                    key,
                    sleep_for,
                )
                time.sleep(max(sleep_for, 0))

        self._last_call[key] = time.time()

    # ------------------------------------------------------------------
    # Called AFTER each response
    # ------------------------------------------------------------------

    def update_from_headers(self, endpoint: str, headers: Dict[str, str]) -> None:
        key = _endpoint_key(endpoint)
        limit_str = headers.get(_H_LIMIT) or headers.get(_H_LIMIT.lower())
        remaining_str = headers.get(_H_REMAINING) or headers.get(_H_REMAINING.lower())
        reset_str = headers.get(_H_RESET) or headers.get(_H_RESET.lower())

        if not (limit_str and remaining_str and reset_str):
            return

        try:
            limit_ = int(limit_str)
            remaining = int(remaining_str)
            reset_at = int(reset_str)
        except (TypeError, ValueError):
            return

        log.debug(
            "Rate-limit for %s: %d/%d, resets at %d", key, remaining, limit_, reset_at
        )
        self._db.update_rate_limit(key, limit_, remaining, reset_at)

    # ------------------------------------------------------------------
    # Manual sleep until a specific endpoint resets
    # ------------------------------------------------------------------

    def sleep_until_reset(self, endpoint: str) -> None:
        key = _endpoint_key(endpoint)
        row = self._db.get_rate_limit(key)
        if row and row["reset_at"]:
            wait = max(row["reset_at"] - time.time() + 2.0, 0.0)
            log.info("Sleeping %.1fs until %s rate-limit resets", wait, key)
            time.sleep(wait)
