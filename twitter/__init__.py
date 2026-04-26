"""
Twitter Automation Library
==========================
A comprehensive Python library for Twitter automation with:
  - Auto Follow (batch, with rate limiting)
  - Auto Quote Tweet (via Twitter API v2)
  - BTC/ETH Alerts (CoinGecko REST + Binance WebSocket)
  - Product Hunt Tracker (Product Hunt API v2)
  - Hacker News Hot Posts (HN Algolia API)
  - Security CVE Alerts (NIST NVD API v2.0)
  - SQLite caching layer
  - Batch processing & scheduling
"""

from twitter.client import TwitterClient
from twitter.modules.auto_follow import AutoFollow
from twitter.modules.quote_tweet import QuoteTweet
from twitter.modules.crypto_alerts import CryptoAlerts
from twitter.modules.product_hunt import ProductHuntTracker
from twitter.modules.hacker_news import HackerNewsTracker
from twitter.modules.cve_alerts import CVEAlerts
from twitter.cache.db import CacheDB
from twitter.utils.rate_limiter import RateLimiter
from twitter.utils.scheduler import TaskScheduler

__version__ = "1.0.0"
__author__ = "twitter-lib"

__all__ = [
    "TwitterClient",
    "AutoFollow",
    "QuoteTweet",
    "CryptoAlerts",
    "ProductHuntTracker",
    "HackerNewsTracker",
    "CVEAlerts",
    "CacheDB",
    "RateLimiter",
    "TaskScheduler",
]
