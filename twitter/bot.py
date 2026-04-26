"""
bot.py — High-level Bot orchestrator.

The Bot class is the recommended entry point for running the full suite
of automation modules. It:

  1. Reads Config (from env / file)
  2. Creates a TwitterClient
  3. Instantiates every module (AutoFollow, QuoteTweet, CryptoAlerts, …)
  4. Registers scheduled tasks via TaskScheduler
  5. Provides convenience one-shot methods for each feature

Typical usage
-------------
    from twitter.bot import Bot
    from twitter.config import Config

    cfg = Config.from_env(".env")
    bot = Bot(cfg)
    bot.start()          # starts background scheduler + crypto WS
    bot.run_forever()    # blocks; Ctrl-C to stop

Or one-shot:
    bot = Bot(cfg)
    bot.run_hacker_news()
    bot.run_cve_alerts()
    bot.close()
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from twitter.client import TwitterClient
from twitter.config import Config
from twitter.modules.auto_follow import AutoFollow
from twitter.modules.cve_alerts import CVEAlerts
from twitter.modules.crypto_alerts import AlertRule, CryptoAlerts
from twitter.modules.hacker_news import HackerNewsTracker
from twitter.modules.product_hunt import ProductHuntTracker
from twitter.modules.quote_tweet import QuoteTweet
from twitter.utils.scheduler import TaskScheduler

log = logging.getLogger(__name__)


class Bot:
    """
    All-in-one Twitter automation bot.

    Parameters
    ----------
    config : Config
        Fully populated configuration object.
    crypto_alert_rules : list of AlertRule, optional
        Pre-built alert rules passed directly to CryptoAlerts.
    """

    def __init__(
        self,
        config: Config,
        crypto_alert_rules: Optional[List[AlertRule]] = None,
    ):
        config.configure_logging()
        config.validate()
        self.config = config

        log.info("Initialising TwitterClient…")
        self.client = TwitterClient(
            api_key=config.credentials.api_key,
            api_secret=config.credentials.api_secret,
            access_token=config.credentials.access_token,
            access_token_secret=config.credentials.access_token_secret,
            bearer_token=config.credentials.bearer_token,
            db_path=config.db_path,
        )

        # --- Module instantiation ---
        af_cfg = config.auto_follow
        self.auto_follow = AutoFollow(
            self.client,
            daily_cap=af_cfg.daily_cap,
            inter_follow_delay=af_cfg.inter_follow_delay,
            dry_run=af_cfg.dry_run,
        )

        qt_cfg = config.quote_tweet
        self.quote_tweet = QuoteTweet(
            self.client,
            daily_cap=qt_cfg.daily_cap,
            inter_quote_delay=qt_cfg.inter_quote_delay,
            dry_run=qt_cfg.dry_run,
            use_quote_api=qt_cfg.use_quote_api,
        )

        cr_cfg = config.crypto
        self.crypto_alerts = CryptoAlerts(
            self.client,
            symbols=cr_cfg.symbols,
            alert_rules=crypto_alert_rules or [],
            coingecko_api_key=cr_cfg.coingecko_api_key or None,
            poll_interval=cr_cfg.poll_interval,
            enable_binance_ws=cr_cfg.enable_binance_ws,
            dry_run=cr_cfg.dry_run,
        )

        ph_cfg = config.product_hunt
        self.product_hunt: Optional[ProductHuntTracker] = None
        if ph_cfg.ph_token:
            self.product_hunt = ProductHuntTracker(
                self.client,
                ph_token=ph_cfg.ph_token,
                min_votes=ph_cfg.min_votes,
                dry_run=ph_cfg.dry_run,
            )
        else:
            log.warning("PRODUCT_HUNT_TOKEN not set — ProductHuntTracker disabled.")

        hn_cfg = config.hacker_news
        self.hacker_news = HackerNewsTracker(
            self.client,
            min_score=hn_cfg.min_score,
            dry_run=hn_cfg.dry_run,
        )

        cve_cfg = config.cve
        self.cve_alerts = CVEAlerts(
            self.client,
            nvd_api_key=cve_cfg.nvd_api_key or None,
            min_cvss_score=cve_cfg.min_cvss_score,
            severities=cve_cfg.severities,
            keywords=cve_cfg.keywords,
            dry_run=cve_cfg.dry_run,
            request_delay=cve_cfg.request_delay,
        )

        self.scheduler = TaskScheduler(tick=1.0)
        self._running = False

    # ------------------------------------------------------------------
    # Scheduler setup
    # ------------------------------------------------------------------

    def _register_tasks(self) -> None:
        """Register all periodic tasks with the scheduler."""

        # Auto-follow (every 6 hours by default)
        # Disabled by default; call bot.enable_auto_follow(...) to activate.
        # self.scheduler.add_interval("auto_follow", self._task_auto_follow, interval=21600)

        # Quote tweets (every 2 hours)
        # self.scheduler.add_interval("quote_tweet", self._task_quote_tweet, interval=7200)

        # Product Hunt (daily at 09:00 UTC)
        if self.product_hunt:
            self.scheduler.add_daily(
                "product_hunt",
                self._task_product_hunt,
                at="09:00",
            )

        # Hacker News (every 3 hours)
        self.scheduler.add_interval(
            "hacker_news",
            self._task_hacker_news,
            interval=10800,
            run_immediately=True,
        )

        # CVE Alerts (every 6 hours)
        self.scheduler.add_interval(
            "cve_alerts",
            self._task_cve_alerts,
            interval=21600,
            run_immediately=False,
        )

        log.info("Scheduled tasks registered. Use scheduler.get_status() to inspect.")

    # ------------------------------------------------------------------
    # Start / stop
    # ------------------------------------------------------------------

    def start(self) -> None:
        """
        Start background threads:
          - CryptoAlerts (CoinGecko poll + optional Binance WS)
          - TaskScheduler (HN, CVE, PH, quote tweets)
        """
        if self._running:
            log.warning("Bot already running")
            return

        log.info("Starting Bot…")
        me = self.client.get_me()
        log.info("Authenticated as @%s (id=%s)", me.get("username"), me.get("id"))

        self.crypto_alerts.start()
        self._register_tasks()
        self.scheduler.start()
        self._running = True
        log.info("Bot started. Crypto polling every %ds.", self.config.crypto.poll_interval)

    def stop(self) -> None:
        log.info("Stopping Bot…")
        self.scheduler.stop()
        self.crypto_alerts.stop()
        self._running = False
        log.info("Bot stopped.")

    def run_forever(self) -> None:
        """Start the bot and block until Ctrl-C."""
        self.start()
        try:
            import time
            while True:
                time.sleep(1)
        except KeyboardInterrupt:
            log.info("Interrupted — shutting down")
        finally:
            self.stop()
            self.close()

    def close(self) -> None:
        self.client.close()
        if self.product_hunt:
            self.product_hunt.close()
        self.hacker_news.close()
        self.cve_alerts.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()

    # ------------------------------------------------------------------
    # One-shot runners (also used as scheduler task callbacks)
    # ------------------------------------------------------------------

    def run_auto_follow(
        self,
        keyword: str,
        max_users: int = 20,
        *,
        min_followers: int = 100,
        max_followers: Optional[int] = 500_000,
        language: str = "en",
    ) -> List[Dict]:
        """Follow users discovered via keyword search."""
        log.info("Running auto-follow for keyword '%s'", keyword)
        return self.auto_follow.follow_by_keyword(
            keyword,
            max_users=max_users,
            min_followers=min_followers,
            max_followers=max_followers,
            language_filter=language,
        )

    def run_follow_list(self, list_id: str, max_users: int = 50) -> List[Dict]:
        """Follow members of a Twitter List."""
        return self.auto_follow.follow_list_members(list_id, max_users=max_users)

    def run_follow_seed(
        self, seed_username: str, max_users: int = 50
    ) -> List[Dict]:
        """Follow followers of a seed account."""
        return self.auto_follow.follow_followers_of(seed_username, max_users=max_users)

    def run_quote_tweets(
        self,
        keyword: str,
        max_quotes: int = 3,
        *,
        topic: str = "default",
        min_likes: int = 10,
        language: str = "en",
        hashtags: Optional[List[str]] = None,
    ) -> List[Dict]:
        """Search for tweets and quote them."""
        log.info("Running quote-tweet for keyword '%s'", keyword)
        return self.quote_tweet.quote_by_keyword(
            keyword,
            max_quotes=max_quotes,
            topic=topic,
            min_likes=min_likes,
            language=language,
            hashtags_to_append=hashtags,
        )

    def run_crypto_fetch(self) -> Dict[str, Dict]:
        """Manually trigger a CoinGecko price fetch + alert evaluation."""
        prices = self.crypto_alerts.fetch_prices_coingecko()
        self.crypto_alerts.evaluate_alerts(prices)
        return prices

    def run_product_hunt(
        self, max_tweets: int = 3, min_votes: Optional[int] = None
    ) -> List[Dict]:
        """Fetch today's Product Hunt posts and tweet the top ones."""
        if not self.product_hunt:
            log.error("ProductHuntTracker not configured (missing PH token)")
            return []
        log.info("Running Product Hunt tracker")
        return self.product_hunt.tweet_top_posts(max_tweets=max_tweets, min_votes=min_votes)

    def run_hacker_news(
        self,
        max_tweets: int = 3,
        min_score: Optional[int] = None,
        category: str = "top",
    ) -> List[Dict]:
        """Fetch HN hot stories and tweet the untweeted ones."""
        log.info("Running Hacker News tracker (category=%s)", category)
        return self.hacker_news.tweet_hot_stories(
            max_tweets=max_tweets, min_score=min_score, category=category
        )

    def run_cve_alerts(
        self,
        hours: int = 24,
        max_tweets: int = 5,
        min_cvss: Optional[float] = None,
    ) -> List[Dict]:
        """Fetch recent CVEs from NVD and tweet the high-severity ones."""
        log.info("Running CVE alert scan (last %dh)", hours)
        return self.cve_alerts.tweet_new_cves(
            hours=hours, max_tweets=max_tweets, min_cvss=min_cvss
        )

    def run_all(self) -> Dict[str, Any]:
        """
        Run all modules once and return a summary dict.
        Useful for testing or scheduled cron-style invocation.
        """
        summary: Dict[str, Any] = {}

        log.info("=== Running all modules ===")

        try:
            prices = self.run_crypto_fetch()
            summary["crypto_prices"] = {s: p.get("price_usd") for s, p in prices.items()}
        except Exception as exc:
            log.error("crypto_fetch failed: %s", exc)
            summary["crypto_prices"] = {"error": str(exc)}

        try:
            hn = self.run_hacker_news(max_tweets=3)
            summary["hacker_news"] = {
                "tweeted": sum(1 for r in hn if r.get("success")),
                "total": len(hn),
            }
        except Exception as exc:
            log.error("hacker_news failed: %s", exc)
            summary["hacker_news"] = {"error": str(exc)}

        try:
            cve = self.run_cve_alerts(hours=24, max_tweets=3)
            summary["cve_alerts"] = {
                "tweeted": sum(1 for r in cve if r.get("success")),
                "total": len(cve),
            }
        except Exception as exc:
            log.error("cve_alerts failed: %s", exc)
            summary["cve_alerts"] = {"error": str(exc)}

        try:
            ph = self.run_product_hunt(max_tweets=2)
            summary["product_hunt"] = {
                "tweeted": sum(1 for r in ph if r.get("success")),
                "total": len(ph),
            }
        except Exception as exc:
            log.error("product_hunt failed: %s", exc)
            summary["product_hunt"] = {"error": str(exc)}

        log.info("=== Run complete: %s ===", summary)
        return summary

    def status(self) -> Dict[str, Any]:
        """Return a live status snapshot of the bot."""
        return {
            "running": self._running,
            "authenticated_as": self.client.get_me(),
            "follow_stats": self.auto_follow.stats(),
            "cached_crypto_prices": self.crypto_alerts.get_latest_prices(),
            "cached_hn_stories": len(self.hacker_news.get_cached_stories()),
            "cached_cves": len(self.cve_alerts.get_cached_cves()),
            "cached_ph_posts": len(
                self.product_hunt.get_cached_posts() if self.product_hunt else []
            ),
            "scheduler_tasks": self.scheduler.get_status(),
        }

    # ------------------------------------------------------------------
    # Internal scheduler callbacks
    # ------------------------------------------------------------------

    def _task_auto_follow(self) -> None:
        pass  # configured per deployment

    def _task_quote_tweet(self) -> None:
        pass  # configured per deployment

    def _task_product_hunt(self) -> None:
        self.run_product_hunt(max_tweets=3)

    def _task_hacker_news(self) -> None:
        self.run_hacker_news(max_tweets=3)

    def _task_cve_alerts(self) -> None:
        self.run_cve_alerts(hours=6, max_tweets=5)

    # ------------------------------------------------------------------
    # Crypto alert helper shortcuts
    # ------------------------------------------------------------------

    def add_btc_above_alert(self, price: float, cooldown: int = 3600) -> None:
        self.crypto_alerts.add_price_above("BTC", price, cooldown=cooldown)

    def add_btc_below_alert(self, price: float, cooldown: int = 3600) -> None:
        self.crypto_alerts.add_price_below("BTC", price, cooldown=cooldown)

    def add_eth_above_alert(self, price: float, cooldown: int = 3600) -> None:
        self.crypto_alerts.add_price_above("ETH", price, cooldown=cooldown)

    def add_eth_below_alert(self, price: float, cooldown: int = 3600) -> None:
        self.crypto_alerts.add_price_below("ETH", price, cooldown=cooldown)

    def add_btc_change_alert(self, pct: float, direction: str = "up") -> None:
        if direction == "up":
            self.crypto_alerts.add_change_pct_up("BTC", pct)
        else:
            self.crypto_alerts.add_change_pct_down("BTC", pct)

    def add_eth_change_alert(self, pct: float, direction: str = "up") -> None:
        if direction == "up":
            self.crypto_alerts.add_change_pct_up("ETH", pct)
        else:
            self.crypto_alerts.add_change_pct_down("ETH", pct)
