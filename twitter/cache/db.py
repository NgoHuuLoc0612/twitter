"""
CacheDB — SQLite-backed persistence layer.

Tables
------
request_log         : full audit trail of every HTTP request
rate_limits         : per-endpoint Twitter rate-limit state
followed_users      : record of every follow action taken
posted_tweets       : record of every tweet posted
crypto_prices       : latest BTC/ETH price snapshots
crypto_alerts_sent  : deduplication for price alerts
product_hunt_cache  : Product Hunt posts cache
hn_cache            : Hacker News posts cache
cve_cache           : CVE records cache
kv_store            : generic key-value store for ad-hoc caching
"""

from __future__ import annotations

import json
import sqlite3
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

_DDL = """
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS request_log (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    ts            REAL    NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    method        TEXT    NOT NULL,
    url           TEXT    NOT NULL,
    status        INTEGER NOT NULL,
    request_body  TEXT,
    response_body TEXT
);

CREATE TABLE IF NOT EXISTS rate_limits (
    endpoint  TEXT PRIMARY KEY,
    limit_    INTEGER,
    remaining INTEGER,
    reset_at  INTEGER,
    updated   REAL NOT NULL DEFAULT (unixepoch('now', 'subsec'))
);

CREATE TABLE IF NOT EXISTS followed_users (
    target_user_id   TEXT PRIMARY KEY,
    target_username  TEXT,
    followed_at      REAL NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    source           TEXT         -- e.g. 'keyword:bitcoin', 'list:12345'
);

CREATE TABLE IF NOT EXISTS posted_tweets (
    tweet_id    TEXT PRIMARY KEY,
    text        TEXT NOT NULL,
    posted_at   REAL NOT NULL DEFAULT (unixepoch('now', 'subsec')),
    tweet_type  TEXT,              -- 'quote', 'original', 'reply'
    source_id   TEXT               -- original tweet id for quote tweets
);

CREATE TABLE IF NOT EXISTS crypto_prices (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    price_usd   REAL NOT NULL,
    change_24h  REAL,
    volume_24h  REAL,
    market_cap  REAL,
    source      TEXT NOT NULL,    -- 'coingecko' | 'binance'
    ts          REAL NOT NULL DEFAULT (unixepoch('now', 'subsec'))
);
CREATE INDEX IF NOT EXISTS idx_crypto_prices_sym_ts ON crypto_prices(symbol, ts DESC);

CREATE TABLE IF NOT EXISTS crypto_alerts_sent (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    symbol      TEXT NOT NULL,
    alert_type  TEXT NOT NULL,    -- 'above', 'below', 'change_pct'
    threshold   REAL NOT NULL,
    price_at    REAL NOT NULL,
    tweet_id    TEXT,
    ts          REAL NOT NULL DEFAULT (unixepoch('now', 'subsec'))
);

CREATE TABLE IF NOT EXISTS product_hunt_cache (
    post_id       TEXT PRIMARY KEY,
    name          TEXT NOT NULL,
    tagline       TEXT,
    description   TEXT,
    url           TEXT,
    votes         INTEGER,
    topics        TEXT,            -- JSON list
    featured_at   TEXT,
    tweeted       INTEGER NOT NULL DEFAULT 0,
    tweeted_at    REAL,
    fetched_at    REAL NOT NULL DEFAULT (unixepoch('now', 'subsec'))
);

CREATE TABLE IF NOT EXISTS hn_cache (
    story_id      INTEGER PRIMARY KEY,
    title         TEXT NOT NULL,
    url           TEXT,
    score         INTEGER,
    author        TEXT,
    comments      INTEGER,
    story_type    TEXT,
    posted_at     INTEGER,         -- Unix timestamp from HN
    tweeted       INTEGER NOT NULL DEFAULT 0,
    tweeted_at    REAL,
    fetched_at    REAL NOT NULL DEFAULT (unixepoch('now', 'subsec'))
);

CREATE TABLE IF NOT EXISTS cve_cache (
    cve_id          TEXT PRIMARY KEY,
    description     TEXT,
    severity        TEXT,
    cvss_score      REAL,
    cvss_vector     TEXT,
    published_date  TEXT,
    modified_date   TEXT,
    references_json TEXT,          -- JSON list of URLs
    affected_json   TEXT,          -- JSON list of CPEs
    tweeted         INTEGER NOT NULL DEFAULT 0,
    tweeted_at      REAL,
    fetched_at      REAL NOT NULL DEFAULT (unixepoch('now', 'subsec'))
);

CREATE TABLE IF NOT EXISTS kv_store (
    key_       TEXT PRIMARY KEY,
    value_     TEXT,
    expires_at REAL,
    updated    REAL NOT NULL DEFAULT (unixepoch('now', 'subsec'))
);
"""


class CacheDB:
    """Thread-safe SQLite wrapper used across the entire library."""

    def __init__(self, path: str = "twitter_cache.db"):
        self.path = path
        self._local = threading.local()
        self._lock = threading.Lock()
        # Initialise schema on the calling thread's connection
        conn = self._conn()
        conn.executescript(_DDL)
        conn.commit()

    def _conn(self) -> sqlite3.Connection:
        if not getattr(self._local, "conn", None):
            conn = sqlite3.connect(self.path, check_same_thread=False)
            conn.row_factory = sqlite3.Row
            self._local.conn = conn
        return self._local.conn

    @property
    def conn(self) -> sqlite3.Connection:
        return self._conn()

    def execute(self, sql: str, params: Tuple = ()) -> sqlite3.Cursor:
        with self._lock:
            cur = self.conn.execute(sql, params)
            self.conn.commit()
            return cur

    def executemany(self, sql: str, params_list: List[Tuple]) -> None:
        with self._lock:
            self.conn.executemany(sql, params_list)
            self.conn.commit()

    def fetchall(self, sql: str, params: Tuple = ()) -> List[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchall()

    def fetchone(self, sql: str, params: Tuple = ()) -> Optional[sqlite3.Row]:
        with self._lock:
            return self.conn.execute(sql, params).fetchone()

    # ------------------------------------------------------------------
    # Request logging
    # ------------------------------------------------------------------

    def log_request(
        self,
        method: str,
        url: str,
        status: int,
        request_body: Optional[str],
        response_body: Optional[str],
    ) -> None:
        self.execute(
            "INSERT INTO request_log (method, url, status, request_body, response_body) "
            "VALUES (?, ?, ?, ?, ?)",
            (method, url, status, request_body, response_body),
        )

    # ------------------------------------------------------------------
    # Rate limits
    # ------------------------------------------------------------------

    def update_rate_limit(
        self, endpoint: str, limit_: int, remaining: int, reset_at: int
    ) -> None:
        self.execute(
            "INSERT INTO rate_limits (endpoint, limit_, remaining, reset_at, updated) "
            "VALUES (?, ?, ?, ?, unixepoch('now', 'subsec')) "
            "ON CONFLICT(endpoint) DO UPDATE SET "
            "  limit_=excluded.limit_, remaining=excluded.remaining, "
            "  reset_at=excluded.reset_at, updated=excluded.updated",
            (endpoint, limit_, remaining, reset_at),
        )

    def get_rate_limit(self, endpoint: str) -> Optional[sqlite3.Row]:
        return self.fetchone(
            "SELECT * FROM rate_limits WHERE endpoint = ?", (endpoint,)
        )

    # ------------------------------------------------------------------
    # Followed users
    # ------------------------------------------------------------------

    def record_follow(
        self, target_user_id: str, target_username: str, source: str
    ) -> None:
        self.execute(
            "INSERT OR IGNORE INTO followed_users "
            "(target_user_id, target_username, source) VALUES (?, ?, ?)",
            (target_user_id, target_username, source),
        )

    def is_followed(self, target_user_id: str) -> bool:
        row = self.fetchone(
            "SELECT 1 FROM followed_users WHERE target_user_id = ?",
            (target_user_id,),
        )
        return row is not None

    def get_follow_count(self) -> int:
        row = self.fetchone("SELECT COUNT(*) as c FROM followed_users")
        return row["c"] if row else 0

    def get_recent_follows(self, limit: int = 50) -> List[sqlite3.Row]:
        return self.fetchall(
            "SELECT * FROM followed_users ORDER BY followed_at DESC LIMIT ?",
            (limit,),
        )

    # ------------------------------------------------------------------
    # Posted tweets
    # ------------------------------------------------------------------

    def record_tweet(
        self,
        tweet_id: str,
        text: str,
        tweet_type: str,
        source_id: Optional[str] = None,
    ) -> None:
        self.execute(
            "INSERT OR IGNORE INTO posted_tweets "
            "(tweet_id, text, tweet_type, source_id) VALUES (?, ?, ?, ?)",
            (tweet_id, text, tweet_type, source_id),
        )

    def is_tweet_posted(self, tweet_id: str) -> bool:
        return (
            self.fetchone(
                "SELECT 1 FROM posted_tweets WHERE tweet_id = ?", (tweet_id,)
            )
            is not None
        )

    # ------------------------------------------------------------------
    # Crypto prices
    # ------------------------------------------------------------------

    def record_price(
        self,
        symbol: str,
        price_usd: float,
        change_24h: Optional[float],
        volume_24h: Optional[float],
        market_cap: Optional[float],
        source: str,
    ) -> None:
        self.execute(
            "INSERT INTO crypto_prices "
            "(symbol, price_usd, change_24h, volume_24h, market_cap, source) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (symbol, price_usd, change_24h, volume_24h, market_cap, source),
        )

    def get_latest_price(self, symbol: str) -> Optional[sqlite3.Row]:
        return self.fetchone(
            "SELECT * FROM crypto_prices WHERE symbol = ? ORDER BY ts DESC LIMIT 1",
            (symbol,),
        )

    def get_price_history(
        self, symbol: str, since_ts: float, limit: int = 500
    ) -> List[sqlite3.Row]:
        return self.fetchall(
            "SELECT * FROM crypto_prices WHERE symbol = ? AND ts >= ? "
            "ORDER BY ts ASC LIMIT ?",
            (symbol, since_ts, limit),
        )

    def record_alert_sent(
        self,
        symbol: str,
        alert_type: str,
        threshold: float,
        price_at: float,
        tweet_id: Optional[str],
    ) -> None:
        self.execute(
            "INSERT INTO crypto_alerts_sent "
            "(symbol, alert_type, threshold, price_at, tweet_id) "
            "VALUES (?, ?, ?, ?, ?)",
            (symbol, alert_type, threshold, price_at, tweet_id),
        )

    def alert_sent_recently(
        self, symbol: str, alert_type: str, threshold: float, within_seconds: int = 3600
    ) -> bool:
        cutoff = time.time() - within_seconds
        row = self.fetchone(
            "SELECT 1 FROM crypto_alerts_sent "
            "WHERE symbol=? AND alert_type=? AND threshold=? AND ts>=?",
            (symbol, alert_type, threshold, cutoff),
        )
        return row is not None

    # ------------------------------------------------------------------
    # Product Hunt cache
    # ------------------------------------------------------------------

    def upsert_ph_post(self, post: Dict) -> None:
        self.execute(
            "INSERT INTO product_hunt_cache "
            "(post_id, name, tagline, description, url, votes, topics, featured_at, fetched_at) "
            "VALUES (:post_id, :name, :tagline, :description, :url, :votes, :topics, "
            ":featured_at, unixepoch('now','subsec')) "
            "ON CONFLICT(post_id) DO UPDATE SET "
            "  name=excluded.name, tagline=excluded.tagline, votes=excluded.votes, "
            "  topics=excluded.topics, fetched_at=excluded.fetched_at",
            post,
        )

    def get_untweeted_ph_posts(self, limit: int = 10) -> List[sqlite3.Row]:
        return self.fetchall(
            "SELECT * FROM product_hunt_cache WHERE tweeted=0 ORDER BY votes DESC LIMIT ?",
            (limit,),
        )

    def mark_ph_tweeted(self, post_id: str, tweet_id: str) -> None:
        self.execute(
            "UPDATE product_hunt_cache SET tweeted=1, tweeted_at=unixepoch('now','subsec') "
            "WHERE post_id=?",
            (post_id,),
        )

    # ------------------------------------------------------------------
    # Hacker News cache
    # ------------------------------------------------------------------

    def upsert_hn_story(self, story: Dict) -> None:
        self.execute(
            "INSERT INTO hn_cache "
            "(story_id, title, url, score, author, comments, story_type, posted_at, fetched_at) "
            "VALUES (:story_id, :title, :url, :score, :author, :comments, "
            ":story_type, :posted_at, unixepoch('now','subsec')) "
            "ON CONFLICT(story_id) DO UPDATE SET "
            "  score=excluded.score, comments=excluded.comments, fetched_at=excluded.fetched_at",
            story,
        )

    def get_untweeted_hn_stories(self, min_score: int = 100, limit: int = 10) -> List[sqlite3.Row]:
        return self.fetchall(
            "SELECT * FROM hn_cache WHERE tweeted=0 AND score>=? "
            "ORDER BY score DESC LIMIT ?",
            (min_score, limit),
        )

    def mark_hn_tweeted(self, story_id: int, tweet_id: str) -> None:
        self.execute(
            "UPDATE hn_cache SET tweeted=1, tweeted_at=unixepoch('now','subsec') WHERE story_id=?",
            (story_id,),
        )

    # ------------------------------------------------------------------
    # CVE cache
    # ------------------------------------------------------------------

    def upsert_cve(self, cve: Dict) -> None:
        self.execute(
            "INSERT INTO cve_cache "
            "(cve_id, description, severity, cvss_score, cvss_vector, "
            "published_date, modified_date, references_json, affected_json, fetched_at) "
            "VALUES (:cve_id, :description, :severity, :cvss_score, :cvss_vector, "
            ":published_date, :modified_date, :references_json, :affected_json, "
            "unixepoch('now','subsec')) "
            "ON CONFLICT(cve_id) DO UPDATE SET "
            "  severity=excluded.severity, cvss_score=excluded.cvss_score, "
            "  modified_date=excluded.modified_date, fetched_at=excluded.fetched_at",
            cve,
        )

    def get_untweeted_cves(
        self, min_score: float = 7.0, limit: int = 10
    ) -> List[sqlite3.Row]:
        return self.fetchall(
            "SELECT * FROM cve_cache WHERE tweeted=0 AND cvss_score>=? "
            "ORDER BY cvss_score DESC LIMIT ?",
            (min_score, limit),
        )

    def mark_cve_tweeted(self, cve_id: str, tweet_id: str) -> None:
        self.execute(
            "UPDATE cve_cache SET tweeted=1, tweeted_at=unixepoch('now','subsec') WHERE cve_id=?",
            (cve_id,),
        )

    # ------------------------------------------------------------------
    # Generic KV store
    # ------------------------------------------------------------------

    def kv_set(self, key: str, value: Any, ttl_seconds: Optional[int] = None) -> None:
        expires_at = (time.time() + ttl_seconds) if ttl_seconds else None
        self.execute(
            "INSERT INTO kv_store (key_, value_, expires_at, updated) "
            "VALUES (?, ?, ?, unixepoch('now','subsec')) "
            "ON CONFLICT(key_) DO UPDATE SET value_=excluded.value_, "
            "expires_at=excluded.expires_at, updated=excluded.updated",
            (key, json.dumps(value), expires_at),
        )

    def kv_get(self, key: str) -> Optional[Any]:
        row = self.fetchone(
            "SELECT value_, expires_at FROM kv_store WHERE key_=?", (key,)
        )
        if row is None:
            return None
        if row["expires_at"] and time.time() > row["expires_at"]:
            self.execute("DELETE FROM kv_store WHERE key_=?", (key,))
            return None
        return json.loads(row["value_"])

    def kv_delete(self, key: str) -> None:
        self.execute("DELETE FROM kv_store WHERE key_=?", (key,))

    def close(self) -> None:
        if getattr(self._local, "conn", None):
            self._local.conn.close()
            self._local.conn = None
