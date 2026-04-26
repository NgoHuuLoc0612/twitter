"""
TwitterClient — core HTTP client wrapping Twitter API v2.
Handles OAuth 1.0a (user-context) and OAuth 2.0 Bearer Token (app-only).
Implements automatic retry with exponential back-off, per-endpoint rate-limit
tracking, and full request/response logging to SQLite.
"""

from __future__ import annotations

import json
import logging
import time
from typing import Any, Dict, List, Optional, Tuple

import requests
from requests.adapters import HTTPAdapter
from requests_oauthlib import OAuth1
from urllib3.util.retry import Retry

from twitter.cache.db import CacheDB
from twitter.utils.rate_limiter import RateLimiter

log = logging.getLogger(__name__)

_BASE = "https://api.twitter.com/2"


class TwitterAPIError(Exception):
    """Raised when the Twitter API returns a non-2xx response."""

    def __init__(self, status: int, body: Any, url: str):
        self.status = status
        self.body = body
        self.url = url
        super().__init__(f"HTTP {status} from {url}: {body}")


class RateLimitError(TwitterAPIError):
    """Raised when the API returns HTTP 429."""

    def __init__(self, reset_at: int, url: str):
        self.reset_at = reset_at
        super(TwitterAPIError, self).__init__(
            f"Rate limited until {reset_at} on {url}"
        )


class TwitterClient:
    """
    Low-level Twitter API v2 client.

    Parameters
    ----------
    api_key : str
        OAuth 1.0a consumer key (API key).
    api_secret : str
        OAuth 1.0a consumer secret.
    access_token : str
        OAuth 1.0a access token (user-context).
    access_token_secret : str
        OAuth 1.0a access token secret.
    bearer_token : str
        OAuth 2.0 Bearer Token for app-only endpoints.
    db_path : str
        Path to SQLite cache/log database.
    max_retries : int
        Number of automatic retries on transient errors.
    backoff_factor : float
        Exponential back-off multiplier between retries.
    """

    def __init__(
        self,
        api_key: str,
        api_secret: str,
        access_token: str,
        access_token_secret: str,
        bearer_token: str,
        db_path: str = "twitter_cache.db",
        max_retries: int = 5,
        backoff_factor: float = 1.5,
    ):
        self.api_key = api_key
        self.api_secret = api_secret
        self.access_token = access_token
        self.access_token_secret = access_token_secret
        self.bearer_token = bearer_token

        self.db = CacheDB(db_path)
        self.rate_limiter = RateLimiter(self.db)

        # HTTP session with connection-level retry (network errors only).
        retry_cfg = Retry(
            total=max_retries,
            backoff_factor=backoff_factor,
            status_forcelist=[500, 502, 503, 504],
            allowed_methods=["GET", "POST", "DELETE", "PUT"],
            raise_on_status=False,
        )
        adapter = HTTPAdapter(max_retries=retry_cfg)
        self._session = requests.Session()
        self._session.mount("https://", adapter)
        self._session.mount("http://", adapter)

        self._me: Optional[Dict] = None

    # ------------------------------------------------------------------
    # OAuth 1.0a signing
    # ------------------------------------------------------------------

    # ------------------------------------------------------------------
    # Core request dispatcher
    # ------------------------------------------------------------------

    def _request(
        self,
        method: str,
        endpoint: str,
        *,
        params: Optional[Dict] = None,
        json_body: Optional[Dict] = None,
        use_oauth1: bool = True,
        timeout: int = 30,
    ) -> Any:
        """
        Execute an HTTP request against the Twitter v2 API.

        Handles:
          - OAuth 1.0a / Bearer Token selection
          - Application-level 429 rate-limit sleeping
          - HTTP-level error raising
          - Response logging to SQLite
        """
        url = endpoint if endpoint.startswith("https://") else f"{_BASE}{endpoint}"

        # Check local rate-limit budget before firing
        self.rate_limiter.wait_if_needed(endpoint)

        if use_oauth1:
            auth = OAuth1(
                self.api_key,
                self.api_secret,
                self.access_token,
                self.access_token_secret,
                signature_type="auth_header",
            )
            headers = {"Content-Type": "application/json"}
        else:
            auth = None
            headers = {
                "Authorization": f"Bearer {self.bearer_token}",
                "Content-Type": "application/json",
            }

        log.debug("→ %s %s params=%s body=%s", method, url, params, json_body)

        resp = self._session.request(
            method,
            url,
            auth=auth,
            headers=headers,
            params=params,
            json=json_body,
            timeout=timeout,
        )

        # Update rate-limit state from response headers
        self.rate_limiter.update_from_headers(endpoint, dict(resp.headers))

        # Log to SQLite
        self.db.log_request(
            method=method,
            url=url,
            status=resp.status_code,
            request_body=json.dumps(json_body) if json_body else None,
            response_body=resp.text[:4096],
        )

        if resp.status_code == 429:
            reset_at = int(resp.headers.get("x-rate-limit-reset", time.time() + 60))
            raise RateLimitError(reset_at=reset_at, url=url)

        if not resp.ok:
            try:
                body = resp.json()
            except Exception:
                body = resp.text
            raise TwitterAPIError(status=resp.status_code, body=body, url=url)

        if resp.status_code == 204 or not resp.text.strip():
            return {}

        return resp.json()

    def get(self, endpoint: str, *, params: Optional[Dict] = None, use_oauth1: bool = True) -> Any:
        return self._request("GET", endpoint, params=params, use_oauth1=use_oauth1)

    def post(self, endpoint: str, *, json_body: Optional[Dict] = None, use_oauth1: bool = True) -> Any:
        return self._request("POST", endpoint, json_body=json_body, use_oauth1=use_oauth1)

    def delete(self, endpoint: str, *, use_oauth1: bool = True) -> Any:
        return self._request("DELETE", endpoint, use_oauth1=use_oauth1)

    # ------------------------------------------------------------------
    # Pagination helper (next_token)
    # ------------------------------------------------------------------

    def paginate(
        self,
        endpoint: str,
        params: Dict,
        *,
        data_key: str = "data",
        max_pages: int = 10,
        use_oauth1: bool = True,
    ):
        """
        Yield pages of results from a paginated endpoint.
        Automatically follows next_token cursors.
        """
        page = 0
        next_token = None
        while page < max_pages:
            if next_token:
                params = {**params, "pagination_token": next_token}
            resp = self.get(endpoint, params=params, use_oauth1=use_oauth1)
            items = resp.get(data_key, [])
            if items:
                yield items
            meta = resp.get("meta", {})
            next_token = meta.get("next_token")
            if not next_token:
                break
            page += 1

    # ------------------------------------------------------------------
    # Convenience: authenticated user
    # ------------------------------------------------------------------

    def get_me(self) -> Dict:
        """Return the authenticated user's profile (cached per session)."""
        if self._me is None:
            resp = self.get(
                "/users/me",
                params={"user.fields": "id,name,username,public_metrics,description"},
            )
            self._me = resp.get("data", {})
        return self._me

    @property
    def my_id(self) -> str:
        return self.get_me()["id"]

    @property
    def my_username(self) -> str:
        return self.get_me()["username"]

    # ------------------------------------------------------------------
    # User lookup
    # ------------------------------------------------------------------

    def get_user_by_username(self, username: str) -> Optional[Dict]:
        username = username.lstrip("@")
        resp = self.get(
            f"/users/by/username/{username}",
            params={"user.fields": "id,name,username,public_metrics,description,verified"},
        )
        return resp.get("data")

    def get_users_by_usernames(self, usernames: List[str]) -> List[Dict]:
        clean = [u.lstrip("@") for u in usernames]
        resp = self.get(
            "/users/by",
            params={
                "usernames": ",".join(clean),
                "user.fields": "id,name,username,public_metrics,description,verified",
            },
        )
        return resp.get("data", [])

    def get_user_by_id(self, user_id: str) -> Optional[Dict]:
        resp = self.get(
            f"/users/{user_id}",
            params={"user.fields": "id,name,username,public_metrics,description,verified"},
        )
        return resp.get("data")

    def search_users_by_keyword(self, keyword: str, max_results: int = 10) -> List[Dict]:
        """
        Search users by keyword using recent tweet search then extract unique authors.
        Twitter API v2 does not offer direct user search for non-enterprise tiers.
        """
        results = []
        seen_ids: set = set()
        for page in self.paginate(
            "/tweets/search/recent",
            params={
                "query": keyword,
                "max_results": min(max_results * 3, 100),
                "expansions": "author_id",
                "user.fields": "id,name,username,public_metrics,verified",
            },
            data_key="data",
            max_pages=3,
        ):
            for tweet in page:
                aid = tweet.get("author_id", "")
                if aid and aid not in seen_ids:
                    seen_ids.add(aid)
                    results.append({"id": aid})
                    if len(results) >= max_results:
                        return results
        return results

    # ------------------------------------------------------------------
    # Tweet operations
    # ------------------------------------------------------------------

    def post_tweet(
        self,
        text: str,
        *,
        reply_to_id: Optional[str] = None,
        quote_tweet_id: Optional[str] = None,
        media_ids: Optional[List[str]] = None,
    ) -> Dict:
        body: Dict[str, Any] = {"text": text}
        if reply_to_id:
            body["reply"] = {"in_reply_to_tweet_id": reply_to_id}
        if quote_tweet_id:
            body["quote_tweet_id"] = quote_tweet_id
        if media_ids:
            body["media"] = {"media_ids": media_ids}
        resp = self.post("/tweets", json_body=body)
        return resp.get("data", resp)

    def delete_tweet(self, tweet_id: str) -> bool:
        resp = self.delete(f"/tweets/{tweet_id}")
        return resp.get("data", {}).get("deleted", False)

    def get_tweet(self, tweet_id: str) -> Optional[Dict]:
        resp = self.get(
            f"/tweets/{tweet_id}",
            params={
                "tweet.fields": "id,text,author_id,created_at,public_metrics,conversation_id",
                "expansions": "author_id",
                "user.fields": "id,name,username",
            },
        )
        return resp.get("data")

    def search_recent_tweets(
        self,
        query: str,
        max_results: int = 10,
        *,
        sort_order: str = "recency",
        tweet_fields: Optional[str] = None,
    ) -> List[Dict]:
        fields = tweet_fields or "id,text,author_id,created_at,public_metrics,lang"
        results = []
        for page in self.paginate(
            "/tweets/search/recent",
            params={
                "query": query,
                "max_results": min(max_results, 100),
                "tweet.fields": fields,
                "sort_order": sort_order,
            },
        ):
            results.extend(page)
            if len(results) >= max_results:
                break
        return results[:max_results]

    def get_user_timeline(
        self,
        user_id: str,
        max_results: int = 10,
        *,
        exclude_retweets: bool = True,
        exclude_replies: bool = False,
    ) -> List[Dict]:
        excludes = []
        if exclude_retweets:
            excludes.append("retweets")
        if exclude_replies:
            excludes.append("replies")
        params: Dict[str, Any] = {
            "max_results": min(max_results, 100),
            "tweet.fields": "id,text,created_at,public_metrics",
        }
        if excludes:
            params["exclude"] = ",".join(excludes)
        results = []
        for page in self.paginate(f"/users/{user_id}/tweets", params=params):
            results.extend(page)
            if len(results) >= max_results:
                break
        return results[:max_results]

    # ------------------------------------------------------------------
    # Follow operations
    # ------------------------------------------------------------------

    def follow_user(self, target_user_id: str) -> Dict:
        return self.post(
            f"/users/{self.my_id}/following",
            json_body={"target_user_id": target_user_id},
        )

    def get_following(self, user_id: Optional[str] = None, max_results: int = 1000) -> List[Dict]:
        uid = user_id or self.my_id
        results = []
        for page in self.paginate(
            f"/users/{uid}/following",
            params={
                "max_results": 1000,
                "user.fields": "id,name,username,public_metrics",
            },
        ):
            results.extend(page)
            if len(results) >= max_results:
                break
        return results[:max_results]

    def get_followers(self, user_id: Optional[str] = None, max_results: int = 1000) -> List[Dict]:
        uid = user_id or self.my_id
        results = []
        for page in self.paginate(
            f"/users/{uid}/followers",
            params={
                "max_results": 1000,
                "user.fields": "id,name,username,public_metrics",
            },
        ):
            results.extend(page)
            if len(results) >= max_results:
                break
        return results[:max_results]

    # ------------------------------------------------------------------
    # Like / Retweet
    # ------------------------------------------------------------------

    def like_tweet(self, tweet_id: str) -> Dict:
        return self.post(
            f"/users/{self.my_id}/likes",
            json_body={"tweet_id": tweet_id},
        )

    def retweet(self, tweet_id: str) -> Dict:
        return self.post(
            f"/users/{self.my_id}/retweets",
            json_body={"tweet_id": tweet_id},
        )

    # ------------------------------------------------------------------
    # Lists
    # ------------------------------------------------------------------

    def get_list_members(self, list_id: str, max_results: int = 100) -> List[Dict]:
        results = []
        for page in self.paginate(
            f"/lists/{list_id}/members",
            params={"max_results": 100, "user.fields": "id,name,username,public_metrics"},
        ):
            results.extend(page)
            if len(results) >= max_results:
                break
        return results[:max_results]

    # ------------------------------------------------------------------
    # Close
    # ------------------------------------------------------------------

    def close(self):
        self._session.close()
        self.db.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()
