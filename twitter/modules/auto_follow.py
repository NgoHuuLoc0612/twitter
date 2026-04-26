"""
AutoFollow — batch follow engine for Twitter API v2.

Features
--------
* Follow users by keyword search (finds authors of recent tweets)
* Follow users from a Twitter List
* Follow followers of a seed account
* Follow from an explicit list of usernames / user IDs
* Full deduplication via SQLite (never follows the same user twice)
* Respects Twitter's follow rate-limit (400 follows/day, 15/15 min)
* Configurable daily cap and batch size
* Dry-run mode
* Detailed follow log stored in SQLite
"""

from __future__ import annotations

import logging
import time
from typing import Dict, List, Optional, TYPE_CHECKING

from twitter.utils.helpers import chunks

if TYPE_CHECKING:
    from twitter.client import TwitterClient

log = logging.getLogger(__name__)

# Twitter enforces 400 follows / 24 h and 15 follows per 15-minute window
_DAILY_LIMIT = 400
_WINDOW_LIMIT = 15          # per 15 minutes
_WINDOW_SECONDS = 15 * 60
_INTER_FOLLOW_DELAY = 65    # seconds between each follow action (safe default)


class AutoFollow:
    """
    Batch follow engine.

    Parameters
    ----------
    client : TwitterClient
        Authenticated Twitter client.
    daily_cap : int
        Maximum follows to perform per 24-hour period (≤ 400).
    inter_follow_delay : float
        Seconds to wait between consecutive follow API calls.
    dry_run : bool
        If True, log what would be done but make no API calls.
    """

    def __init__(
        self,
        client: "TwitterClient",
        daily_cap: int = 50,
        inter_follow_delay: float = _INTER_FOLLOW_DELAY,
        dry_run: bool = False,
    ):
        self.client = client
        self.daily_cap = min(daily_cap, _DAILY_LIMIT)
        self.inter_follow_delay = inter_follow_delay
        self.dry_run = dry_run
        self._db = client.db

    # ------------------------------------------------------------------
    # Public entry points
    # ------------------------------------------------------------------

    def follow_by_keyword(
        self,
        keyword: str,
        max_users: int = 20,
        *,
        min_followers: int = 0,
        max_followers: Optional[int] = None,
        require_verified: bool = False,
        language_filter: Optional[str] = None,
    ) -> List[Dict]:
        """
        Search recent tweets for `keyword`, collect unique authors,
        and follow them (subject to filters and daily cap).

        Parameters
        ----------
        keyword : str
            Twitter search query (supports operators: -is:retweet lang:en …).
        max_users : int
            Target number of users to follow in this run.
        min_followers : int
            Skip accounts with fewer followers than this.
        max_followers : int, optional
            Skip accounts with more followers than this (avoid bots / celebs).
        require_verified : bool
            Only follow verified accounts.
        language_filter : str, optional
            ISO 639-1 language code (e.g. 'en', 'ja').

        Returns
        -------
        list of dicts with follow result per user.
        """
        log.info("AutoFollow.follow_by_keyword('%s') max_users=%d", keyword, max_users)
        source_tag = f"keyword:{keyword}"

        # Build tweet search query
        query = keyword
        if language_filter:
            query += f" lang:{language_filter}"
        query += " -is:retweet"

        # Gather candidate user IDs via tweet search (expansions: author_id)
        candidate_ids: List[str] = []
        seen: set = set()

        for page in self.client.paginate(
            "/tweets/search/recent",
            params={
                "query": query,
                "max_results": 100,
                "expansions": "author_id",
                "user.fields": "id,name,username,public_metrics,verified",
            },
            max_pages=5,
        ):
            for tweet in page:
                aid = tweet.get("author_id", "")
                if aid and aid not in seen:
                    seen.add(aid)
                    candidate_ids.append(aid)
                if len(candidate_ids) >= max_users * 5:
                    break

        # Fetch full user objects in batches of 100
        candidates: List[Dict] = []
        for batch in chunks(candidate_ids, 100):
            joined = ",".join(batch)
            resp = self.client.get(
                "/users",
                params={
                    "ids": joined,
                    "user.fields": "id,name,username,public_metrics,verified",
                },
            )
            candidates.extend(resp.get("data", []))

        # Apply filters
        filtered = self._filter_users(
            candidates,
            min_followers=min_followers,
            max_followers=max_followers,
            require_verified=require_verified,
        )

        return self._execute_follow_batch(filtered[:max_users], source=source_tag)

    def follow_list_members(
        self,
        list_id: str,
        max_users: int = 50,
        *,
        min_followers: int = 0,
        max_followers: Optional[int] = None,
    ) -> List[Dict]:
        """Follow members of a Twitter List."""
        log.info("AutoFollow.follow_list_members(list_id=%s)", list_id)
        members = self.client.get_list_members(list_id, max_results=max_users * 2)
        filtered = self._filter_users(
            members, min_followers=min_followers, max_followers=max_followers
        )
        return self._execute_follow_batch(filtered[:max_users], source=f"list:{list_id}")

    def follow_followers_of(
        self,
        seed_username: str,
        max_users: int = 50,
        *,
        min_followers: int = 0,
        max_followers: Optional[int] = None,
    ) -> List[Dict]:
        """Follow followers of `seed_username`."""
        log.info("AutoFollow.follow_followers_of('%s')", seed_username)
        user = self.client.get_user_by_username(seed_username)
        if not user:
            log.error("User @%s not found", seed_username)
            return []
        followers = self.client.get_followers(user["id"], max_results=max_users * 2)
        filtered = self._filter_users(
            followers, min_followers=min_followers, max_followers=max_followers
        )
        return self._execute_follow_batch(filtered[:max_users], source=f"followers_of:{seed_username}")

    def follow_by_usernames(
        self, usernames: List[str]
    ) -> List[Dict]:
        """Follow an explicit list of usernames."""
        log.info("AutoFollow.follow_by_usernames(%d users)", len(usernames))
        users = self.client.get_users_by_usernames(usernames)
        return self._execute_follow_batch(users, source="explicit_list")

    def follow_by_user_ids(self, user_ids: List[str]) -> List[Dict]:
        """Follow an explicit list of user IDs."""
        log.info("AutoFollow.follow_by_user_ids(%d users)", len(user_ids))
        users = []
        for batch in chunks(user_ids, 100):
            resp = self.client.get(
                "/users",
                params={"ids": ",".join(batch), "user.fields": "id,name,username,public_metrics"},
            )
            users.extend(resp.get("data", []))
        return self._execute_follow_batch(users, source="explicit_ids")

    # ------------------------------------------------------------------
    # Stats
    # ------------------------------------------------------------------

    def stats(self) -> Dict:
        return {
            "total_followed": self._db.get_follow_count(),
            "recent_follows": [
                dict(r) for r in self._db.get_recent_follows(limit=10)
            ],
        }

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _filter_users(
        self,
        users: List[Dict],
        *,
        min_followers: int = 0,
        max_followers: Optional[int] = None,
        require_verified: bool = False,
    ) -> List[Dict]:
        filtered = []
        for user in users:
            uid = user.get("id", "")
            metrics = user.get("public_metrics", {})
            followers = metrics.get("followers_count", 0)

            # Skip self
            if uid == self.client.my_id:
                continue

            # Already followed
            if self._db.is_followed(uid):
                log.debug("Skip @%s — already followed", user.get("username", uid))
                continue

            if followers < min_followers:
                continue
            if max_followers and followers > max_followers:
                continue
            if require_verified and not user.get("verified", False):
                continue

            filtered.append(user)
        return filtered

    def _check_daily_cap(self) -> int:
        """Return how many follows remain in today's cap."""
        # Count follows in the last 24 hours
        import time as _time
        since = _time.time() - 86400
        rows = self._db.fetchall(
            "SELECT COUNT(*) as c FROM followed_users WHERE followed_at >= ?", (since,)
        )
        used = rows[0]["c"] if rows else 0
        return max(0, self.daily_cap - used)

    def _execute_follow_batch(
        self, users: List[Dict], source: str
    ) -> List[Dict]:
        remaining_cap = self._check_daily_cap()
        if remaining_cap == 0:
            log.warning("Daily follow cap (%d) reached. Skipping.", self.daily_cap)
            return []

        to_follow = users[:remaining_cap]
        results = []

        for user in to_follow:
            uid = str(user.get("id", ""))
            username = user.get("username", uid)

            if self._db.is_followed(uid):
                log.debug("Skip @%s — already in DB", username)
                continue

            result: Dict = {
                "user_id": uid,
                "username": username,
                "action": "follow",
                "dry_run": self.dry_run,
                "success": False,
                "error": None,
            }

            if self.dry_run:
                log.info("[DRY-RUN] Would follow @%s", username)
                result["success"] = True
                self._db.record_follow(uid, username, f"dryrun:{source}")
            else:
                try:
                    resp = self.client.follow_user(uid)
                    result["success"] = True
                    result["response"] = resp
                    self._db.record_follow(uid, username, source)
                    log.info("✓ Followed @%s (id=%s)", username, uid)
                    time.sleep(self.inter_follow_delay)
                except Exception as exc:
                    result["error"] = str(exc)
                    log.error("✗ Failed to follow @%s: %s", username, exc)

            results.append(result)

        log.info(
            "Follow batch done. Attempted: %d, Success: %d",
            len(results),
            sum(1 for r in results if r["success"]),
        )
        return results
