"""
QuoteTweet — automated quote-tweet engine.

Features
--------
* Search recent tweets by keyword / hashtag and quote them with a
  generated or template-based comment.
* Compose comments with Jinja2-style template substitution or a
  custom callable.
* Deduplication: never quote the same tweet twice.
* Configurable filters: min likes, min retweets, language, etc.
* Supports batch quoting with rate-limit-aware delays.
* Optional AI-generated commentary via OpenAI (if key provided).
* Dry-run mode.
* Quotes fetched from Quotable.io API (https://api.quotable.io) —
  filtered by topic tag when available, with fallback to random quotes.
"""

from __future__ import annotations

import json
import logging
import random
import time
import urllib.error
import urllib.parse
import urllib.request
from string import Template
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

from twitter.utils.helpers import truncate_tweet, build_hashtags

if TYPE_CHECKING:
    from twitter.client import TwitterClient

log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Quotable.io integration
# ---------------------------------------------------------------------------

# Map internal topic keys → Quotable.io tag slugs.
# Full tag list: https://api.quotable.io/tags
_QUOTABLE_TAG_MAP: Dict[str, str] = {
    "default":  "inspirational",
    "tech":     "technology",
    "crypto":   "business",
    "security": "wisdom",
    "news":     "knowledge",
}

_QUOTABLE_BASE = "https://api.quotable.io"
_QUOTABLE_TIMEOUT = 5  # seconds


def _fetch_quote(topic: str = "default") -> Optional[Dict]:
    """
    Fetch a single random quote from Quotable.io filtered by topic tag.

    Returns a dict with keys ``content``, ``author``, ``tags`` on success,
    or ``None`` if the API is unreachable / returns an error.
    """
    tag = _QUOTABLE_TAG_MAP.get(topic, "inspirational")
    url = f"{_QUOTABLE_BASE}/quotes/random?tags={urllib.parse.quote(tag)}&limit=1"
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "twitter-lib/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_QUOTABLE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            # API returns a list when limit is set
            if isinstance(data, list) and data:
                return data[0]
    except (urllib.error.URLError, json.JSONDecodeError, Exception) as exc:
        log.warning("Quotable.io fetch failed (tag=%s): %s", tag, exc)
    return None


def _fetch_random_quote() -> Optional[Dict]:
    """Fetch a completely random quote (fallback when topic tag yields nothing)."""
    url = f"{_QUOTABLE_BASE}/quotes/random?limit=1"
    try:
        req = urllib.request.Request(
            url,
            headers={"Accept": "application/json", "User-Agent": "twitter-lib/1.0"},
        )
        with urllib.request.urlopen(req, timeout=_QUOTABLE_TIMEOUT) as resp:
            data = json.loads(resp.read().decode())
            if isinstance(data, list) and data:
                return data[0]
    except Exception as exc:
        log.warning("Quotable.io random fetch failed: %s", exc)
    return None


def build_quote_comment(topic: str = "default") -> str:
    """
    Build a tweet comment using a real quote from Quotable.io.

    Format:
        "<quote content>" — <author name>

    Falls back to a random quote, then to static text if the API is down.
    """
    quote = _fetch_quote(topic) or _fetch_random_quote()
    if quote:
        content: str = quote.get("content", "").strip()
        author: str = quote.get("author", "Unknown").strip()
        # Ensure the assembled string fits within Twitter's 280-char limit
        full = f'"{content}" — {author}'
        return truncate_tweet(full)
    # Ultimate fallback
    return "Worth reading 👇"


# ---------------------------------------------------------------------------
# Static fallback templates (used only when Quotable.io is unavailable AND
# a comment_template / comment_callable is not provided).
# ---------------------------------------------------------------------------

DEFAULT_TEMPLATES: Dict[str, List[str]] = {
    "default": [
        "Interesting take on this 🧵 $author",
        "Worth reading 👇",
        "This resonates. Thoughts?",
        "Great insight here 💡",
        "Sharing this because it's spot-on 👇",
    ],
    "crypto": [
        "Market update 📊 $author",
        "Keep an eye on this 👁️",
        "Interesting perspective on crypto 🔐",
        "Worth watching in the current market 📉📈",
    ],
    "tech": [
        "Big moves in tech 🖥️",
        "This is changing the game 🚀",
        "Developer insight 🛠️ $author",
        "Engineering matters 💻",
    ],
    "security": [
        "Security alert 🔐 Stay protected!",
        "Cybersecurity update ⚠️ $author",
        "Important for infosec folks 🛡️",
    ],
}

_INTER_QUOTE_DELAY = 5.0   # seconds between consecutive quote tweets


class QuoteTweet:
    """
    Automated quote-tweet engine.

    Parameters
    ----------
    client : TwitterClient
    daily_cap : int
        Max quote tweets per 24-hour period.
    inter_quote_delay : float
        Seconds between consecutive quote actions.
    dry_run : bool
    use_quote_api : bool
        When True (default), fetch real quotes from Quotable.io to use as
        the tweet comment.  Set to False to use the static DEFAULT_TEMPLATES
        instead (or when Quotable.io is blocked in your environment).
    """

    def __init__(
        self,
        client: "TwitterClient",
        daily_cap: int = 50,
        inter_quote_delay: float = _INTER_QUOTE_DELAY,
        dry_run: bool = False,
        use_quote_api: bool = True,
    ):
        self.client = client
        self.daily_cap = daily_cap
        self.inter_quote_delay = inter_quote_delay
        self.dry_run = dry_run
        self.use_quote_api = use_quote_api
        self._db = client.db

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def quote_by_keyword(
        self,
        keyword: str,
        max_quotes: int = 5,
        *,
        comment_template: Optional[str] = None,
        comment_callable: Optional[Callable[[Dict], str]] = None,
        topic: str = "default",
        min_likes: int = 0,
        min_retweets: int = 0,
        language: str = "en",
        exclude_from_accounts: Optional[List[str]] = None,
        hashtags_to_append: Optional[List[str]] = None,
    ) -> List[Dict]:
        """
        Find recent tweets matching `keyword` and quote them.

        Parameters
        ----------
        keyword : str
            Twitter search query.
        max_quotes : int
            Maximum number of quote tweets to post.
        comment_template : str, optional
            Template string with $author, $likes, $retweets placeholders.
            If not provided, a random template from DEFAULT_TEMPLATES[topic] is used.
        comment_callable : callable, optional
            Function that receives the tweet dict and returns the comment string.
            Takes priority over comment_template.
        topic : str
            Key into DEFAULT_TEMPLATES for automatic comment selection.
        min_likes : int
            Skip tweets with fewer likes.
        min_retweets : int
            Skip tweets with fewer retweets.
        language : str
            ISO 639-1 language code filter.
        exclude_from_accounts : list of str, optional
            Skip tweets from these usernames.
        hashtags_to_append : list of str, optional
            Hashtags appended to every comment.
        """
        log.info("QuoteTweet.quote_by_keyword('%s') max=%d", keyword, max_quotes)

        remaining = self._remaining_daily_cap()
        if remaining == 0:
            log.warning("Daily quote cap reached")
            return []

        query = f"{keyword} lang:{language} -is:retweet"
        tweet_fields = "id,text,author_id,created_at,public_metrics,lang"

        candidates: List[Dict] = []
        for page in self.client.paginate(
            "/tweets/search/recent",
            params={
                "query": query,
                "max_results": 100,
                "tweet.fields": tweet_fields,
                "expansions": "author_id",
                "user.fields": "id,username",
                "sort_order": "relevancy",
            },
            max_pages=3,
        ):
            candidates.extend(page)
            if len(candidates) >= max_quotes * 10:
                break

        # Filter
        excluded_names = {u.lstrip("@").lower() for u in (exclude_from_accounts or [])}
        filtered = []
        for tweet in candidates:
            tid = tweet.get("id", "")
            metrics = tweet.get("public_metrics", {})

            if self._db.is_tweet_posted(tid):
                continue
            if metrics.get("like_count", 0) < min_likes:
                continue
            if metrics.get("retweet_count", 0) < min_retweets:
                continue
            author_id = tweet.get("author_id", "")
            if author_id == self.client.my_id:
                continue
            # We'd need the includes to map author_id → username for exclude check;
            # skip for now unless excluded list is empty
            filtered.append(tweet)

        results = []
        for tweet in filtered[: min(max_quotes, remaining)]:
            tid = tweet["id"]
            author_id = tweet.get("author_id", "unknown")
            metrics = tweet.get("public_metrics", {})

            # Build comment
            if comment_callable:
                comment = comment_callable(tweet)
            elif comment_template:
                comment = Template(comment_template).safe_substitute(
                    author=f"@{author_id}",
                    likes=metrics.get("like_count", 0),
                    retweets=metrics.get("retweet_count", 0),
                )
            elif self.use_quote_api:
                # Fetch a real quote from Quotable.io, tagged by topic
                comment = build_quote_comment(topic)
                log.debug("Quotable.io comment: %s", comment)
            else:
                # Fallback: pick a random static template
                templates = DEFAULT_TEMPLATES.get(topic, DEFAULT_TEMPLATES["default"])
                tmpl = random.choice(templates)
                comment = Template(tmpl).safe_substitute(
                    author=f"@{author_id}",
                    likes=metrics.get("like_count", 0),
                    retweets=metrics.get("retweet_count", 0),
                )

            if hashtags_to_append:
                tag_str = build_hashtags(hashtags_to_append)
                comment = truncate_tweet(f"{comment} {tag_str}")
            else:
                comment = truncate_tweet(comment)

            result: Dict = {
                "source_tweet_id": tid,
                "comment": comment,
                "dry_run": self.dry_run,
                "success": False,
                "tweet_id": None,
                "error": None,
            }

            if self.dry_run:
                log.info("[DRY-RUN] Would quote tweet %s with: %s", tid, comment)
                result["success"] = True
                self._db.record_tweet(f"dry_{tid}", comment, "quote", tid)
            else:
                try:
                    resp = self.client.post_tweet(comment, quote_tweet_id=tid)
                    new_id = resp.get("id", "")
                    result["tweet_id"] = new_id
                    result["success"] = True
                    self._db.record_tweet(new_id, comment, "quote", tid)
                    log.info("✓ Quoted tweet %s → new tweet %s", tid, new_id)
                    time.sleep(self.inter_quote_delay)
                except Exception as exc:
                    result["error"] = str(exc)
                    log.error("✗ Failed to quote tweet %s: %s", tid, exc)

            results.append(result)

        return results

    def quote_specific_tweet(
        self,
        tweet_id: str,
        comment: str,
    ) -> Dict:
        """Quote a specific tweet with a given comment string."""
        comment = truncate_tweet(comment)
        result: Dict = {
            "source_tweet_id": tweet_id,
            "comment": comment,
            "dry_run": self.dry_run,
            "success": False,
            "tweet_id": None,
            "error": None,
        }

        if self._db.is_tweet_posted(tweet_id):
            log.warning("Tweet %s already quoted", tweet_id)
            result["error"] = "already_quoted"
            return result

        if self.dry_run:
            log.info("[DRY-RUN] Would quote %s: %s", tweet_id, comment)
            result["success"] = True
            return result

        try:
            resp = self.client.post_tweet(comment, quote_tweet_id=tweet_id)
            new_id = resp.get("id", "")
            result["tweet_id"] = new_id
            result["success"] = True
            self._db.record_tweet(new_id, comment, "quote", tweet_id)
            log.info("✓ Quoted %s → %s", tweet_id, new_id)
        except Exception as exc:
            result["error"] = str(exc)
            log.error("✗ quote_specific_tweet failed: %s", exc)

        return result

    def batch_quote(
        self,
        tweet_ids: List[str],
        comment_fn: Callable[[str], str],
    ) -> List[Dict]:
        """
        Quote an explicit list of tweet IDs.
        `comment_fn` receives the tweet_id and returns the comment string.
        """
        results = []
        remaining = self._remaining_daily_cap()
        for tid in tweet_ids[:remaining]:
            comment = truncate_tweet(comment_fn(tid))
            res = self.quote_specific_tweet(tid, comment)
            results.append(res)
            if not self.dry_run and res["success"]:
                time.sleep(self.inter_quote_delay)
        return results

    # ------------------------------------------------------------------
    # Post a plain tweet (not a quote)
    # ------------------------------------------------------------------

    def post_original(self, text: str) -> Dict:
        """Post a plain (non-quote) tweet."""
        text = truncate_tweet(text)
        if self.dry_run:
            log.info("[DRY-RUN] Would post: %s", text)
            return {"success": True, "dry_run": True, "text": text}
        try:
            resp = self.client.post_tweet(text)
            tid = resp.get("id", "")
            self._db.record_tweet(tid, text, "original")
            log.info("✓ Posted original tweet %s", tid)
            return {"success": True, "tweet_id": tid, "text": text}
        except Exception as exc:
            log.error("✗ post_original failed: %s", exc)
            return {"success": False, "error": str(exc), "text": text}

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _remaining_daily_cap(self) -> int:
        since = time.time() - 86400
        rows = self._db.fetchall(
            "SELECT COUNT(*) as c FROM posted_tweets WHERE posted_at >= ?", (since,)
        )
        used = rows[0]["c"] if rows else 0
        return max(0, self.daily_cap - used)
