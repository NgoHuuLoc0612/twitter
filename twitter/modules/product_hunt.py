"""
ProductHuntTracker — fetches today's top posts from Product Hunt API v2
and optionally tweets about them.

Product Hunt API v2
-------------------
- GraphQL endpoint: https://api.producthunt.com/v2/api/graphql
- Auth: Bearer token (Developer Token from https://www.producthunt.com/v2/oauth/applications)
- Rate limit: ~500 req/hour

Features
--------
* Fetch today's featured posts with votes, comments, topics, makers
* Filter by minimum votes, topic keywords, or day offset
* Cache posts in SQLite (dedup)
* Auto-tweet untweeted posts with configurable template
* Support pagination for exhaustive collection
* Batch mode: collect N days of history
"""

from __future__ import annotations

import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, TYPE_CHECKING

import requests

from twitter.utils.helpers import truncate_tweet, build_hashtags

if TYPE_CHECKING:
    from twitter.client import TwitterClient

log = logging.getLogger(__name__)

_GQL_ENDPOINT = "https://api.producthunt.com/v2/api/graphql"

_POSTS_QUERY = """
query GetPosts($after: String, $order: PostsOrder, $postedAfter: DateTime, $postedBefore: DateTime, $topic: String, $first: Int) {
  posts(
    after: $after
    order: $order
    postedAfter: $postedAfter
    postedBefore: $postedBefore
    topic: $topic
    first: $first
  ) {
    pageInfo {
      hasNextPage
      endCursor
    }
    edges {
      node {
        id
        name
        tagline
        description
        url
        votesCount
        commentsCount
        featuredAt
        website
        topics {
          edges {
            node {
              name
              slug
            }
          }
        }
        makers {
          id
          username
          name
          twitterUsername
        }
        thumbnail {
          url
        }
      }
    }
  }
}
"""


class ProductHuntTracker:
    """
    Tracks Product Hunt posts and optionally auto-tweets them.

    Parameters
    ----------
    client : TwitterClient
    ph_token : str
        Product Hunt Developer Token (Bearer).
    min_votes : int
        Only tweet posts with at least this many votes.
    tweet_template : str, optional
        Template string. Available variables: {name}, {tagline}, {url},
        {votes}, {comments}, {topics}, {makers_twitter}.
    dry_run : bool
    """

    def __init__(
        self,
        client: "TwitterClient",
        ph_token: str,
        min_votes: int = 50,
        tweet_template: Optional[str] = None,
        dry_run: bool = False,
    ):
        self.client = client
        self.ph_token = ph_token
        self.min_votes = min_votes
        self.dry_run = dry_run
        self._db = client.db
        self._session = requests.Session()
        self._session.headers.update(
            {
                "Authorization": f"Bearer {ph_token}",
                "Content-Type": "application/json",
                "Accept": "application/json",
            }
        )
        self.tweet_template = tweet_template or (
            "🚀 {name} just launched on @ProductHunt!\n\n"
            "{tagline}\n\n"
            "▲ {votes} votes | 💬 {comments}\n"
            "{topics}\n\n"
            "{url}"
        )

    # ------------------------------------------------------------------
    # GraphQL fetch
    # ------------------------------------------------------------------

    def _graphql(self, query: str, variables: Dict) -> Dict:
        payload = {"query": query, "variables": variables}
        resp = self._session.post(_GQL_ENDPOINT, json=payload, timeout=20)
        if not resp.ok:
            log.error("Product Hunt API error %d: %s", resp.status_code, resp.text[:500])
            resp.raise_for_status()
        data = resp.json()
        if "errors" in data:
            log.error("GraphQL errors: %s", data["errors"])
            raise RuntimeError(f"Product Hunt GraphQL error: {data['errors']}")
        return data.get("data", {})

    # ------------------------------------------------------------------
    # Fetch posts
    # ------------------------------------------------------------------

    def fetch_posts(
        self,
        date: Optional[datetime] = None,
        topic: Optional[str] = None,
        order: str = "VOTES",
        max_posts: int = 50,
    ) -> List[Dict]:
        """
        Fetch Product Hunt posts for a given date.

        Parameters
        ----------
        date : datetime, optional
            Date to fetch posts for (UTC). Defaults to today.
        topic : str, optional
            Topic slug to filter by (e.g. 'developer-tools').
        order : str
            'VOTES' | 'NEWEST' | 'DAILY_RANK'
        max_posts : int
            Maximum posts to return.

        Returns
        -------
        list of post dicts (also cached in SQLite)
        """
        if date is None:
            date = datetime.now(timezone.utc)

        # Start of day / end of day UTC
        day_start = date.replace(hour=0, minute=0, second=0, microsecond=0)
        day_end = day_start + timedelta(days=1)

        posts: List[Dict] = []
        cursor: Optional[str] = None

        while len(posts) < max_posts:
            variables: Dict[str, Any] = {
                "first": min(20, max_posts - len(posts)),
                "order": order,
                "postedAfter": day_start.isoformat(),
                "postedBefore": day_end.isoformat(),
            }
            if cursor:
                variables["after"] = cursor
            if topic:
                variables["topic"] = topic

            try:
                data = self._graphql(_POSTS_QUERY, variables)
            except Exception as exc:
                log.error("ProductHunt fetch failed: %s", exc)
                break

            ph_data = data.get("posts", {})
            edges = ph_data.get("edges", [])
            page_info = ph_data.get("pageInfo", {})

            for edge in edges:
                node = edge.get("node", {})
                post = self._normalise_post(node)
                posts.append(post)
                self._db.upsert_ph_post(self._post_for_db(post))

            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")

        log.info(
            "Fetched %d Product Hunt posts for %s",
            len(posts),
            date.strftime("%Y-%m-%d"),
        )
        return posts

    def fetch_posts_batch(
        self,
        days: int = 7,
        topic: Optional[str] = None,
        min_votes: Optional[int] = None,
    ) -> List[Dict]:
        """Fetch posts for the last N days."""
        all_posts: List[Dict] = []
        for i in range(days):
            date = datetime.now(timezone.utc) - timedelta(days=i)
            posts = self.fetch_posts(date=date, topic=topic, max_posts=50)
            mv = min_votes if min_votes is not None else self.min_votes
            filtered = [p for p in posts if p["votes"] >= mv]
            all_posts.extend(filtered)
        return all_posts

    # ------------------------------------------------------------------
    # Tweet untweeted posts
    # ------------------------------------------------------------------

    def tweet_top_posts(
        self,
        date: Optional[datetime] = None,
        max_tweets: int = 3,
        min_votes: Optional[int] = None,
    ) -> List[Dict]:
        """
        Fetch today's top posts and tweet any that haven't been tweeted yet.

        Returns list of tweet result dicts.
        """
        mv = min_votes if min_votes is not None else self.min_votes

        # Ensure DB is fresh
        self.fetch_posts(date=date)

        # Get untweeted posts from DB
        rows = self._db.get_untweeted_ph_posts(limit=max_tweets * 2)
        results = []

        for row in rows:
            post = dict(row)
            if post.get("votes", 0) < mv:
                continue

            topics_list = json.loads(post.get("topics") or "[]")
            hashtags = build_hashtags(topics_list + ["ProductHunt"])

            tweet_text = self.tweet_template.format(
                name=post["name"],
                tagline=post.get("tagline", ""),
                url=post.get("url", ""),
                votes=post.get("votes", 0),
                comments="",
                topics=hashtags,
                makers_twitter="",
            )
            tweet_text = truncate_tweet(tweet_text)

            result: Dict[str, Any] = {
                "post_id": post["post_id"],
                "name": post["name"],
                "votes": post["votes"],
                "tweet_text": tweet_text,
                "tweet_id": None,
                "dry_run": self.dry_run,
                "success": False,
            }

            if self.dry_run:
                log.info("[DRY-RUN] Would tweet PH post '%s': %s", post["name"], tweet_text[:80])
                result["success"] = True
                self._db.mark_ph_tweeted(post["post_id"], "dry_run")
            else:
                try:
                    resp = self.client.post_tweet(tweet_text)
                    tid = resp.get("id", "")
                    result["tweet_id"] = tid
                    result["success"] = True
                    self._db.mark_ph_tweeted(post["post_id"], tid)
                    log.info("✓ Tweeted PH post '%s' → %s", post["name"], tid)
                    time.sleep(3)
                except Exception as exc:
                    result["error"] = str(exc)
                    log.error("✗ Failed to tweet PH post '%s': %s", post["name"], exc)

            results.append(result)
            if len(results) >= max_tweets:
                break

        return results

    def get_cached_posts(self, limit: int = 20, min_votes: int = 0) -> List[Dict]:
        """Return posts from the SQLite cache."""
        rows = self._db.fetchall(
            "SELECT * FROM product_hunt_cache WHERE votes >= ? ORDER BY votes DESC LIMIT ?",
            (min_votes, limit),
        )
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    def _normalise_post(self, node: Dict) -> Dict:
        topics = [
            e["node"]["name"]
            for e in node.get("topics", {}).get("edges", [])
        ]
        makers = node.get("makers", [])
        maker_twitters = [
            f"@{m['twitterUsername']}"
            for m in makers
            if m.get("twitterUsername")
        ]
        return {
            "id": node.get("id", ""),
            "name": node.get("name", ""),
            "tagline": node.get("tagline", ""),
            "description": node.get("description", ""),
            "url": node.get("url", ""),
            "votes": node.get("votesCount", 0),
            "comments": node.get("commentsCount", 0),
            "featured_at": node.get("featuredAt", ""),
            "website": node.get("website", ""),
            "topics": topics,
            "maker_twitters": maker_twitters,
            "thumbnail": node.get("thumbnail", {}).get("url", ""),
        }

    def _post_for_db(self, post: Dict) -> Dict:
        return {
            "post_id": post["id"],
            "name": post["name"],
            "tagline": post.get("tagline", ""),
            "description": post.get("description", ""),
            "url": post.get("url", ""),
            "votes": post.get("votes", 0),
            "topics": json.dumps(post.get("topics", [])),
            "featured_at": post.get("featured_at", ""),
        }

    def close(self) -> None:
        self._session.close()
