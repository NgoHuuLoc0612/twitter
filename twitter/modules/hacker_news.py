"""
HackerNewsTracker — fetches hot Hacker News posts and tweets them.

APIs used
---------
1. HN Firebase API (official):
   https://hacker-news.firebaseio.com/v0/
   - /topstories.json   : IDs of top 500 stories
   - /beststories.json  : IDs of best stories
   - /newstories.json   : IDs of newest stories
   - /item/{id}.json    : full story/comment object

2. Algolia HN Search API (for full-text search):
   https://hn.algolia.com/api/v1/search
   - Supports searching by query, tags, date range
   - Includes hit score, comments, author, URL

Features
--------
* Fetch top/best/new stories from official HN API
* Search HN by keyword via Algolia
* Filter by minimum score, story type (story/ask/show), date
* Cache in SQLite with dedup
* Auto-tweet untweeted stories above score threshold
* Batch processing: collect and tweet multiple stories per run
* Configurable tweet template
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, TYPE_CHECKING

import requests

from twitter.utils.helpers import truncate_tweet, format_large_number

if TYPE_CHECKING:
    from twitter.client import TwitterClient

log = logging.getLogger(__name__)

_HN_BASE = "https://hacker-news.firebaseio.com/v0"
_ALGOLIA_BASE = "https://hn.algolia.com/api/v1"

_STORY_TYPES = {"story", "ask", "show", "job", "poll"}

_DEFAULT_TWEET_TMPL = (
    "🔥 [{score} pts] {title}\n\n"
    "{url_line}"
    "💬 {comments} comments → {hn_url}\n"
    "#HackerNews #tech"
)


class HackerNewsTracker:
    """
    Track Hacker News hot posts and auto-tweet them.

    Parameters
    ----------
    client : TwitterClient
    min_score : int
        Minimum HN score to consider a story for tweeting.
    tweet_template : str, optional
        Template with {score}, {title}, {url_line}, {comments},
        {hn_url}, {author} placeholders.
    dry_run : bool
    """

    def __init__(
        self,
        client: "TwitterClient",
        min_score: int = 100,
        tweet_template: Optional[str] = None,
        dry_run: bool = False,
    ):
        self.client = client
        self.min_score = min_score
        self.tweet_template = tweet_template or _DEFAULT_TWEET_TMPL
        self.dry_run = dry_run
        self._db = client.db
        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"

    # ------------------------------------------------------------------
    # HN Firebase API
    # ------------------------------------------------------------------

    def fetch_top_story_ids(self, category: str = "top", limit: int = 100) -> List[int]:
        """
        category : 'top' | 'best' | 'new' | 'ask' | 'show' | 'job'
        """
        endpoint_map = {
            "top": "topstories",
            "best": "beststories",
            "new": "newstories",
            "ask": "askstories",
            "show": "showstories",
            "job": "jobstories",
        }
        endpoint = endpoint_map.get(category, "topstories")
        try:
            resp = self._session.get(f"{_HN_BASE}/{endpoint}.json", timeout=15)
            resp.raise_for_status()
            ids = resp.json() or []
            return ids[:limit]
        except Exception as exc:
            log.error("HN fetch_top_story_ids failed: %s", exc)
            return []

    def fetch_item(self, item_id: int) -> Optional[Dict]:
        """Fetch a single HN item (story, comment, etc.) by ID."""
        try:
            resp = self._session.get(f"{_HN_BASE}/item/{item_id}.json", timeout=10)
            resp.raise_for_status()
            return resp.json()
        except Exception as exc:
            log.debug("HN fetch_item(%d) failed: %s", item_id, exc)
            return None

    def fetch_stories(
        self,
        category: str = "top",
        limit: int = 30,
        *,
        min_score: Optional[int] = None,
        story_types: Optional[List[str]] = None,
    ) -> List[Dict]:
        """
        Fetch and parse top/best/new stories from HN Firebase API.

        Parameters
        ----------
        category : str
            'top' | 'best' | 'new'
        limit : int
            Max stories to return.
        min_score : int, optional
            Filter by minimum score (overrides self.min_score if provided).
        story_types : list of str, optional
            Filter story types, e.g. ['story', 'ask'].

        Returns
        -------
        List of story dicts, cached in SQLite.
        """
        ms = min_score if min_score is not None else self.min_score
        allowed_types = set(story_types) if story_types else {"story", "ask", "show"}

        ids = self.fetch_top_story_ids(category, limit=limit * 3)
        stories: List[Dict] = []

        for sid in ids:
            if len(stories) >= limit:
                break
            item = self.fetch_item(sid)
            if not item:
                continue
            item_type = item.get("type", "story")
            if item_type not in allowed_types:
                continue
            score = item.get("score", 0)
            if score < ms:
                continue
            if item.get("deleted") or item.get("dead"):
                continue

            story = self._normalise_item(item)
            stories.append(story)
            self._db.upsert_hn_story(self._story_for_db(story))
            time.sleep(0.05)  # gentle rate-limit on Firebase API

        log.info("HN: fetched %d stories (category=%s, min_score=%d)", len(stories), category, ms)
        return stories

    # ------------------------------------------------------------------
    # Algolia search
    # ------------------------------------------------------------------

    def search_algolia(
        self,
        query: str,
        tags: str = "story",
        num_results: int = 10,
        *,
        min_points: Optional[int] = None,
        days_ago: int = 1,
    ) -> List[Dict]:
        """
        Search Hacker News via Algolia full-text search API.

        Parameters
        ----------
        query : str
        tags : str
            Algolia tag filter: 'story' | 'ask_hn' | 'show_hn' | 'front_page'
        num_results : int
        min_points : int, optional
        days_ago : int
            Only return stories from the last N days.
        """
        import time as _time
        numeric_filters = []
        if min_points:
            numeric_filters.append(f"points>={min_points}")
        cutoff = int(_time.time()) - days_ago * 86400
        numeric_filters.append(f"created_at_i>={cutoff}")

        params: Dict = {
            "query": query,
            "tags": tags,
            "hitsPerPage": num_results,
        }
        if numeric_filters:
            params["numericFilters"] = ",".join(numeric_filters)

        try:
            resp = self._session.get(
                f"{_ALGOLIA_BASE}/search", params=params, timeout=15
            )
            resp.raise_for_status()
            hits = resp.json().get("hits", [])
        except Exception as exc:
            log.error("HN Algolia search failed: %s", exc)
            return []

        stories = []
        for hit in hits:
            story = {
                "id": int(hit.get("objectID", 0)),
                "title": hit.get("title", ""),
                "url": hit.get("url", ""),
                "score": hit.get("points", 0),
                "author": hit.get("author", ""),
                "comments": hit.get("num_comments", 0),
                "story_type": "story",
                "posted_at": hit.get("created_at_i", 0),
                "hn_url": f"https://news.ycombinator.com/item?id={hit.get('objectID', '')}",
            }
            stories.append(story)
            self._db.upsert_hn_story(self._story_for_db(story))

        return stories

    # ------------------------------------------------------------------
    # Tweet untweeted stories
    # ------------------------------------------------------------------

    def tweet_hot_stories(
        self,
        max_tweets: int = 3,
        min_score: Optional[int] = None,
        *,
        category: str = "top",
        fetch_fresh: bool = True,
    ) -> List[Dict]:
        """
        Fetch (optionally) and tweet untweeted stories above min_score.

        Returns list of tweet result dicts.
        """
        ms = min_score if min_score is not None else self.min_score

        if fetch_fresh:
            self.fetch_stories(category=category, limit=50, min_score=ms)

        rows = self._db.get_untweeted_hn_stories(min_score=ms, limit=max_tweets * 2)
        results = []

        for row in rows:
            story = dict(row)
            sid = story["story_id"]

            url_line = f"{story['url']}\n" if story.get("url") else ""
            hn_url = f"https://news.ycombinator.com/item?id={sid}"

            tweet_text = self.tweet_template.format(
                score=story.get("score", 0),
                title=story.get("title", ""),
                url_line=url_line,
                comments=story.get("comments", 0),
                hn_url=hn_url,
                author=story.get("author", ""),
            )
            tweet_text = truncate_tweet(tweet_text)

            result: Dict = {
                "story_id": sid,
                "title": story.get("title", ""),
                "score": story.get("score", 0),
                "tweet_text": tweet_text,
                "tweet_id": None,
                "dry_run": self.dry_run,
                "success": False,
            }

            if self.dry_run:
                log.info("[DRY-RUN] Would tweet HN story %d '%s'", sid, story.get("title", "")[:50])
                result["success"] = True
                self._db.mark_hn_tweeted(sid, "dry_run")
            else:
                try:
                    resp = self.client.post_tweet(tweet_text)
                    tid = resp.get("id", "")
                    result["tweet_id"] = tid
                    result["success"] = True
                    self._db.mark_hn_tweeted(sid, tid)
                    log.info("✓ Tweeted HN story %d → tweet %s", sid, tid)
                    time.sleep(3)
                except Exception as exc:
                    result["error"] = str(exc)
                    log.error("✗ Failed to tweet HN story %d: %s", sid, exc)

            results.append(result)
            if len(results) >= max_tweets:
                break

        return results

    def get_cached_stories(
        self, min_score: int = 0, limit: int = 20, tweeted: Optional[bool] = None
    ) -> List[Dict]:
        """Return stories from the SQLite cache."""
        where_parts = ["score >= ?"]
        params: list = [min_score]
        if tweeted is not None:
            where_parts.append("tweeted = ?")
            params.append(1 if tweeted else 0)
        where = " AND ".join(where_parts)
        rows = self._db.fetchall(
            f"SELECT * FROM hn_cache WHERE {where} ORDER BY score DESC LIMIT ?",
            tuple(params) + (limit,),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _normalise_item(self, item: Dict) -> Dict:
        sid = item.get("id", 0)
        return {
            "id": sid,
            "title": item.get("title", ""),
            "url": item.get("url", ""),
            "score": item.get("score", 0),
            "author": item.get("by", ""),
            "comments": item.get("descendants", 0),
            "story_type": item.get("type", "story"),
            "posted_at": item.get("time", 0),
            "hn_url": f"https://news.ycombinator.com/item?id={sid}",
        }

    def _story_for_db(self, story: Dict) -> Dict:
        return {
            "story_id": story["id"],
            "title": story.get("title", ""),
            "url": story.get("url", ""),
            "score": story.get("score", 0),
            "author": story.get("author", ""),
            "comments": story.get("comments", 0),
            "story_type": story.get("story_type", "story"),
            "posted_at": story.get("posted_at", 0),
        }

    def close(self) -> None:
        self._session.close()
