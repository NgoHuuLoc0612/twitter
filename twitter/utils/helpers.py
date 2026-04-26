"""Miscellaneous helper utilities used across the library."""

from __future__ import annotations

import logging
import re
import time
import unicodedata
from typing import Any, Callable, Generator, Iterable, List, TypeVar

log = logging.getLogger(__name__)

T = TypeVar("T")

TWEET_MAX_LEN = 280


def truncate_tweet(text: str, suffix: str = "…", max_len: int = TWEET_MAX_LEN) -> str:
    """Truncate `text` to fit within Twitter's character limit."""
    if len(text) <= max_len:
        return text
    return text[: max_len - len(suffix)].rstrip() + suffix


def format_large_number(n: float) -> str:
    """Format a large number as a human-readable string (e.g. 1_234_567 → '1.23M')."""
    abs_n = abs(n)
    if abs_n >= 1_000_000_000:
        return f"{n / 1_000_000_000:.2f}B"
    if abs_n >= 1_000_000:
        return f"{n / 1_000_000:.2f}M"
    if abs_n >= 1_000:
        return f"{n / 1_000:.2f}K"
    return str(int(n))


def format_price(price: float) -> str:
    """Format a crypto price with appropriate precision."""
    if price >= 1_000:
        return f"${price:,.2f}"
    if price >= 1:
        return f"${price:.4f}"
    return f"${price:.8f}"


def format_pct(pct: float, include_sign: bool = True) -> str:
    sign = "+" if (include_sign and pct > 0) else ""
    return f"{sign}{pct:.2f}%"


def slugify(text: str) -> str:
    """Convert text to URL-safe slug."""
    text = unicodedata.normalize("NFKD", text)
    text = text.encode("ascii", "ignore").decode("ascii")
    text = re.sub(r"[^\w\s-]", "", text).strip().lower()
    return re.sub(r"[-\s]+", "-", text)


def chunks(lst: List[T], n: int) -> Generator[List[T], None, None]:
    """Yield successive n-sized chunks from lst."""
    for i in range(0, len(lst), n):
        yield lst[i : i + n]


def retry_on_exception(
    func: Callable,
    *args,
    exceptions: tuple = (Exception,),
    max_retries: int = 3,
    backoff: float = 2.0,
    **kwargs,
) -> Any:
    """
    Call `func(*args, **kwargs)` retrying on `exceptions` up to
    `max_retries` times with exponential back-off.
    """
    last_exc: Exception = RuntimeError("No attempts made")
    for attempt in range(max_retries + 1):
        try:
            return func(*args, **kwargs)
        except exceptions as exc:  # type: ignore[misc]
            last_exc = exc
            if attempt < max_retries:
                wait = backoff ** attempt
                log.warning(
                    "Attempt %d/%d failed (%s). Retrying in %.1fs…",
                    attempt + 1,
                    max_retries + 1,
                    exc,
                    wait,
                )
                time.sleep(wait)
    raise last_exc


def build_hashtags(tags: Iterable[str], max_tags: int = 3) -> str:
    """Convert a list of tag strings to '#tag1 #tag2' format."""
    cleaned = []
    for tag in tags:
        t = re.sub(r"[^\w]", "", tag.replace(" ", "_"))
        if t:
            cleaned.append(f"#{t}")
        if len(cleaned) >= max_tags:
            break
    return " ".join(cleaned)
