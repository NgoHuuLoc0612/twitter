#!/usr/bin/env python3
"""
twitter-cli — Command-line interface for the twitter automation library.

Usage
-----
    twitter-cli status
    twitter-cli follow keyword "bitcoin developer" --max 20
    twitter-cli follow list <list_id> --max 50
    twitter-cli follow seed <username> --max 30
    twitter-cli quote keyword "AI news" --max 5 --topic tech
    twitter-cli crypto fetch
    twitter-cli crypto history BTC --hours 24
    twitter-cli crypto alert add BTC above 100000
    twitter-cli producthunt fetch --days 1
    twitter-cli producthunt tweet --max 3
    twitter-cli hackernews fetch --category top --min-score 200
    twitter-cli hackernews tweet --max 3
    twitter-cli cve fetch --hours 24 --min-cvss 9.0
    twitter-cli cve search --keyword "remote code execution"
    twitter-cli cve tweet --max 5
    twitter-cli run all
    twitter-cli db stats
    twitter-cli scheduler status

All commands read credentials from environment variables or a .env file
specified with --env-file.
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
from typing import List, Optional

log = logging.getLogger(__name__)


def _get_bot(args):
    """Build a Bot from config, honouring --env-file and --dry-run."""
    from twitter.config import Config
    from twitter.bot import Bot

    env_file = getattr(args, "env_file", None) or os.environ.get("TWITTER_ENV_FILE", ".env")
    cfg = Config.from_env(dotenv_path=env_file)

    if getattr(args, "dry_run", False):
        cfg.dry_run = True
        cfg.auto_follow.dry_run = True
        cfg.quote_tweet.dry_run = True
        cfg.crypto.dry_run = True
        cfg.product_hunt.dry_run = True
        cfg.hacker_news.dry_run = True
        cfg.cve.dry_run = True

    return Bot(cfg)


def _print_json(obj) -> None:
    print(json.dumps(obj, indent=2, default=str))


# ---------------------------------------------------------------------------
# Sub-command handlers
# ---------------------------------------------------------------------------

def cmd_status(args) -> None:
    bot = _get_bot(args)
    try:
        _print_json(bot.status())
    finally:
        bot.close()


def cmd_follow(args) -> None:
    bot = _get_bot(args)
    try:
        if args.follow_cmd == "keyword":
            results = bot.run_auto_follow(
                args.query,
                max_users=args.max,
                min_followers=args.min_followers,
                max_followers=args.max_followers,
                language=args.lang,
            )
        elif args.follow_cmd == "list":
            results = bot.run_follow_list(args.list_id, max_users=args.max)
        elif args.follow_cmd == "seed":
            results = bot.run_follow_seed(args.username, max_users=args.max)
        elif args.follow_cmd == "usernames":
            results = bot.auto_follow.follow_by_usernames(args.usernames)
        else:
            print(f"Unknown follow subcommand: {args.follow_cmd}", file=sys.stderr)
            sys.exit(1)

        ok = sum(1 for r in results if r.get("success"))
        print(f"Followed {ok}/{len(results)} users")
        if args.verbose:
            _print_json(results)
    finally:
        bot.close()


def cmd_quote(args) -> None:
    bot = _get_bot(args)
    try:
        results = bot.run_quote_tweets(
            args.query,
            max_quotes=args.max,
            topic=args.topic,
            min_likes=args.min_likes,
            language=args.lang,
            hashtags=args.hashtags.split(",") if args.hashtags else None,
        )
        ok = sum(1 for r in results if r.get("success"))
        print(f"Quoted {ok}/{len(results)} tweets")
        if args.verbose:
            _print_json(results)
    finally:
        bot.close()


def cmd_crypto(args) -> None:
    bot = _get_bot(args)
    try:
        if args.crypto_cmd == "fetch":
            prices = bot.run_crypto_fetch()
            _print_json(prices)

        elif args.crypto_cmd == "history":
            history = bot.crypto_alerts.get_price_history(
                args.symbol.upper(), hours=args.hours
            )
            _print_json(history)

        elif args.crypto_cmd == "alert":
            if args.alert_action == "add":
                sym = args.symbol.upper()
                direction = args.direction.lower()
                threshold = float(args.threshold)
                if direction == "above":
                    bot.crypto_alerts.add_price_above(sym, threshold)
                elif direction == "below":
                    bot.crypto_alerts.add_price_below(sym, threshold)
                elif direction == "change_up":
                    bot.crypto_alerts.add_change_pct_up(sym, threshold)
                elif direction == "change_down":
                    bot.crypto_alerts.add_change_pct_down(sym, threshold)
                print(f"Added alert: {sym} {direction} {threshold}")

                # Immediately evaluate after adding
                prices = bot.crypto_alerts.fetch_prices_coingecko()
                triggered = bot.crypto_alerts.evaluate_alerts(prices)
                if triggered:
                    print(f"Alert triggered immediately: {triggered}")

            elif args.alert_action == "list":
                for rule in bot.crypto_alerts.alert_rules:
                    print(
                        f"  [{rule.symbol}] {rule.alert_type} @ {rule.threshold} "
                        f"(cooldown={rule.cooldown_seconds}s, enabled={rule.enabled})"
                    )
    finally:
        bot.close()


def cmd_producthunt(args) -> None:
    bot = _get_bot(args)
    try:
        if not bot.product_hunt:
            print("ProductHuntTracker not configured. Set PRODUCT_HUNT_TOKEN.", file=sys.stderr)
            sys.exit(1)

        if args.ph_cmd == "fetch":
            from datetime import datetime, timezone, timedelta
            date = datetime.now(timezone.utc) - timedelta(days=args.days_ago)
            posts = bot.product_hunt.fetch_posts(date=date, max_posts=args.max)
            print(f"Fetched {len(posts)} posts")
            if args.verbose:
                _print_json(posts)

        elif args.ph_cmd == "tweet":
            results = bot.run_product_hunt(max_tweets=args.max, min_votes=args.min_votes)
            ok = sum(1 for r in results if r.get("success"))
            print(f"Tweeted {ok}/{len(results)} Product Hunt posts")
            if args.verbose:
                _print_json(results)

        elif args.ph_cmd == "cache":
            posts = bot.product_hunt.get_cached_posts(limit=args.max, min_votes=args.min_votes or 0)
            _print_json(posts)
    finally:
        bot.close()


def cmd_hackernews(args) -> None:
    bot = _get_bot(args)
    try:
        if args.hn_cmd == "fetch":
            stories = bot.hacker_news.fetch_stories(
                category=args.category,
                limit=args.max,
                min_score=args.min_score,
            )
            print(f"Fetched {len(stories)} stories")
            if args.verbose:
                for s in stories[:10]:
                    print(f"  [{s['score']}] {s['title'][:70]}")

        elif args.hn_cmd == "search":
            stories = bot.hacker_news.search_algolia(
                query=args.query,
                num_results=args.max,
                min_points=args.min_score,
                days_ago=args.days,
            )
            print(f"Found {len(stories)} stories")
            if args.verbose:
                _print_json(stories)

        elif args.hn_cmd == "tweet":
            results = bot.run_hacker_news(
                max_tweets=args.max,
                min_score=args.min_score,
                category=args.category,
            )
            ok = sum(1 for r in results if r.get("success"))
            print(f"Tweeted {ok}/{len(results)} HN stories")
            if args.verbose:
                _print_json(results)

        elif args.hn_cmd == "cache":
            stories = bot.hacker_news.get_cached_stories(
                min_score=args.min_score or 0, limit=args.max
            )
            _print_json(stories)
    finally:
        bot.close()


def cmd_cve(args) -> None:
    bot = _get_bot(args)
    try:
        if args.cve_cmd == "fetch":
            cves = bot.cve_alerts.fetch_recent_cves(
                hours=args.hours,
                modified=args.modified,
                max_results=args.max,
            )
            print(f"Fetched {len(cves)} CVEs")
            if args.verbose:
                for c in cves[:10]:
                    print(f"  [{c['cvss_score']:.1f} {c['severity']}] {c['id']}: {c['description'][:60]}")

        elif args.cve_cmd == "search":
            cves = bot.cve_alerts.search_cves(
                keyword=args.keyword,
                cpe_name=args.cpe,
                cwe_id=args.cwe,
                cvss_severity=args.severity,
                max_results=args.max,
            )
            print(f"Found {len(cves)} CVEs")
            if args.verbose:
                _print_json(cves)

        elif args.cve_cmd == "get":
            cve = bot.cve_alerts.fetch_cve_by_id(args.cve_id)
            _print_json(cve)

        elif args.cve_cmd == "tweet":
            results = bot.run_cve_alerts(
                hours=args.hours,
                max_tweets=args.max,
                min_cvss=args.min_cvss,
            )
            ok = sum(1 for r in results if r.get("success"))
            print(f"Tweeted {ok}/{len(results)} CVEs")
            if args.verbose:
                _print_json(results)

        elif args.cve_cmd == "cache":
            cves = bot.cve_alerts.get_cached_cves(
                min_score=args.min_cvss or 0.0,
                severity=args.severity,
                tweeted=None,
                limit=args.max,
            )
            _print_json(cves)
    finally:
        bot.close()


def cmd_run(args) -> None:
    bot = _get_bot(args)
    try:
        if args.run_cmd == "all":
            summary = bot.run_all()
            _print_json(summary)
        elif args.run_cmd == "forever":
            bot.run_forever()
    finally:
        if args.run_cmd != "forever":
            bot.close()


def cmd_db(args) -> None:
    from twitter.config import Config
    from twitter.cache.db import CacheDB
    env_file = getattr(args, "env_file", None) or ".env"
    cfg = Config.from_env(dotenv_path=env_file)
    db = CacheDB(cfg.db_path)

    if args.db_cmd == "stats":
        stats = {
            "followed_users": db.fetchone("SELECT COUNT(*) as c FROM followed_users")["c"],
            "posted_tweets": db.fetchone("SELECT COUNT(*) as c FROM posted_tweets")["c"],
            "crypto_prices": db.fetchone("SELECT COUNT(*) as c FROM crypto_prices")["c"],
            "crypto_alerts_sent": db.fetchone("SELECT COUNT(*) as c FROM crypto_alerts_sent")["c"],
            "product_hunt_posts": db.fetchone("SELECT COUNT(*) as c FROM product_hunt_cache")["c"],
            "hn_stories": db.fetchone("SELECT COUNT(*) as c FROM hn_cache")["c"],
            "cve_records": db.fetchone("SELECT COUNT(*) as c FROM cve_cache")["c"],
            "request_log": db.fetchone("SELECT COUNT(*) as c FROM request_log")["c"],
        }
        _print_json(stats)

    elif args.db_cmd == "follows":
        rows = db.get_recent_follows(limit=args.limit)
        _print_json([dict(r) for r in rows])

    elif args.db_cmd == "tweets":
        rows = db.fetchall(
            "SELECT * FROM posted_tweets ORDER BY posted_at DESC LIMIT ?", (args.limit,)
        )
        _print_json([dict(r) for r in rows])

    elif args.db_cmd == "requests":
        rows = db.fetchall(
            "SELECT id, ts, method, url, status FROM request_log ORDER BY ts DESC LIMIT ?",
            (args.limit,),
        )
        _print_json([dict(r) for r in rows])

    db.close()


def cmd_scheduler(args) -> None:
    bot = _get_bot(args)
    try:
        bot.start()
        import time
        time.sleep(2)
        status = bot.scheduler.get_status()
        _print_json(status)
    finally:
        bot.stop()
        bot.close()


# ---------------------------------------------------------------------------
# Argument parser
# ---------------------------------------------------------------------------

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="twitter-cli",
        description="Twitter automation CLI",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--env-file", default=".env", help="Path to .env file")
    parser.add_argument("--dry-run", action="store_true", help="Simulate actions without posting")
    parser.add_argument("--verbose", "-v", action="store_true", help="Print full output")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])

    sub = parser.add_subparsers(dest="command", required=True)

    # --- status ---
    sub.add_parser("status", help="Show bot status")

    # --- follow ---
    p_follow = sub.add_parser("follow", help="Auto-follow users")
    follow_sub = p_follow.add_subparsers(dest="follow_cmd", required=True)

    p_fk = follow_sub.add_parser("keyword", help="Follow users by keyword")
    p_fk.add_argument("query", help="Search query")
    p_fk.add_argument("--max", type=int, default=20)
    p_fk.add_argument("--min-followers", type=int, default=100)
    p_fk.add_argument("--max-followers", type=int, default=500000)
    p_fk.add_argument("--lang", default="en")

    p_fl = follow_sub.add_parser("list", help="Follow Twitter List members")
    p_fl.add_argument("list_id")
    p_fl.add_argument("--max", type=int, default=50)

    p_fs = follow_sub.add_parser("seed", help="Follow followers of a seed account")
    p_fs.add_argument("username")
    p_fs.add_argument("--max", type=int, default=30)

    p_fu = follow_sub.add_parser("usernames", help="Follow explicit list of usernames")
    p_fu.add_argument("usernames", nargs="+")

    # --- quote ---
    p_quote = sub.add_parser("quote", help="Auto quote-tweet")
    quote_sub = p_quote.add_subparsers(dest="quote_cmd", required=True)
    p_qk = quote_sub.add_parser("keyword", help="Quote tweets matching keyword")
    p_qk.add_argument("query")
    p_qk.add_argument("--max", type=int, default=3)
    p_qk.add_argument("--topic", default="default", choices=["default", "crypto", "tech", "security"])
    p_qk.add_argument("--min-likes", type=int, default=10)
    p_qk.add_argument("--lang", default="en")
    p_qk.add_argument("--hashtags", default="", help="Comma-separated hashtags to append")

    # --- crypto ---
    p_crypto = sub.add_parser("crypto", help="Crypto price alerts")
    crypto_sub = p_crypto.add_subparsers(dest="crypto_cmd", required=True)
    crypto_sub.add_parser("fetch", help="Fetch current prices and evaluate alerts")

    p_ch = crypto_sub.add_parser("history", help="Show price history")
    p_ch.add_argument("symbol", help="e.g. BTC")
    p_ch.add_argument("--hours", type=int, default=24)

    p_ca = crypto_sub.add_parser("alert", help="Manage alert rules")
    ca_sub = p_ca.add_subparsers(dest="alert_action", required=True)
    p_ca_add = ca_sub.add_parser("add", help="Add an alert rule")
    p_ca_add.add_argument("symbol", help="BTC / ETH / etc.")
    p_ca_add.add_argument("direction", choices=["above", "below", "change_up", "change_down"])
    p_ca_add.add_argument("threshold", help="Price or percentage threshold")
    ca_sub.add_parser("list", help="List configured alert rules")

    # --- producthunt ---
    p_ph = sub.add_parser("producthunt", help="Product Hunt tracker")
    ph_sub = p_ph.add_subparsers(dest="ph_cmd", required=True)

    p_phf = ph_sub.add_parser("fetch", help="Fetch posts")
    p_phf.add_argument("--days-ago", type=int, default=0)
    p_phf.add_argument("--max", type=int, default=20)

    p_pht = ph_sub.add_parser("tweet", help="Tweet top posts")
    p_pht.add_argument("--max", type=int, default=3)
    p_pht.add_argument("--min-votes", type=int, default=None)

    p_phc = ph_sub.add_parser("cache", help="Show cached posts")
    p_phc.add_argument("--max", type=int, default=20)
    p_phc.add_argument("--min-votes", type=int, default=0)

    # --- hackernews ---
    p_hn = sub.add_parser("hackernews", help="Hacker News tracker")
    hn_sub = p_hn.add_subparsers(dest="hn_cmd", required=True)

    p_hnf = hn_sub.add_parser("fetch", help="Fetch stories from HN")
    p_hnf.add_argument("--category", default="top", choices=["top", "best", "new", "ask", "show"])
    p_hnf.add_argument("--max", type=int, default=30)
    p_hnf.add_argument("--min-score", type=int, default=100)

    p_hns = hn_sub.add_parser("search", help="Search HN via Algolia")
    p_hns.add_argument("query")
    p_hns.add_argument("--max", type=int, default=10)
    p_hns.add_argument("--min-score", type=int, default=50)
    p_hns.add_argument("--days", type=int, default=1)

    p_hnt = hn_sub.add_parser("tweet", help="Tweet untweeted stories")
    p_hnt.add_argument("--max", type=int, default=3)
    p_hnt.add_argument("--min-score", type=int, default=100)
    p_hnt.add_argument("--category", default="top")

    p_hnc = hn_sub.add_parser("cache", help="Show cached stories")
    p_hnc.add_argument("--max", type=int, default=20)
    p_hnc.add_argument("--min-score", type=int, default=0)

    # --- cve ---
    p_cve = sub.add_parser("cve", help="CVE security alerts")
    cve_sub = p_cve.add_subparsers(dest="cve_cmd", required=True)

    p_cvef = cve_sub.add_parser("fetch", help="Fetch recent CVEs from NVD")
    p_cvef.add_argument("--hours", type=int, default=24)
    p_cvef.add_argument("--max", type=int, default=200)
    p_cvef.add_argument("--modified", action="store_true", help="Use lastModified filter")

    p_cves = cve_sub.add_parser("search", help="Search CVEs by keyword / CPE / CWE")
    p_cves.add_argument("--keyword", default=None)
    p_cves.add_argument("--cpe", default=None, dest="cpe")
    p_cves.add_argument("--cwe", default=None)
    p_cves.add_argument("--severity", default=None, choices=["CRITICAL", "HIGH", "MEDIUM", "LOW"])
    p_cves.add_argument("--max", type=int, default=50)

    p_cveg = cve_sub.add_parser("get", help="Fetch a specific CVE by ID")
    p_cveg.add_argument("cve_id", help="e.g. CVE-2021-44228")

    p_cvet = cve_sub.add_parser("tweet", help="Tweet untweeted high-severity CVEs")
    p_cvet.add_argument("--hours", type=int, default=24)
    p_cvet.add_argument("--max", type=int, default=5)
    p_cvet.add_argument("--min-cvss", type=float, default=None)

    p_cvec = cve_sub.add_parser("cache", help="Show cached CVEs")
    p_cvec.add_argument("--max", type=int, default=20)
    p_cvec.add_argument("--min-cvss", type=float, default=0.0)
    p_cvec.add_argument("--severity", default=None)

    # --- run ---
    p_run = sub.add_parser("run", help="Run modules")
    run_sub = p_run.add_subparsers(dest="run_cmd", required=True)
    run_sub.add_parser("all", help="Run all modules once")
    run_sub.add_parser("forever", help="Run forever with scheduler")

    # --- db ---
    p_db = sub.add_parser("db", help="Database utilities")
    db_sub = p_db.add_subparsers(dest="db_cmd", required=True)
    db_sub.add_parser("stats", help="Show DB statistics")
    p_dbf = db_sub.add_parser("follows", help="Show recent follows")
    p_dbf.add_argument("--limit", type=int, default=20)
    p_dbt = db_sub.add_parser("tweets", help="Show posted tweets")
    p_dbt.add_argument("--limit", type=int, default=20)
    p_dbr = db_sub.add_parser("requests", help="Show request log")
    p_dbr.add_argument("--limit", type=int, default=50)

    # --- scheduler ---
    sub.add_parser("scheduler", help="Show scheduler task status")

    return parser


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

_COMMAND_MAP = {
    "status": cmd_status,
    "follow": cmd_follow,
    "quote": cmd_quote,
    "crypto": cmd_crypto,
    "producthunt": cmd_producthunt,
    "hackernews": cmd_hackernews,
    "cve": cmd_cve,
    "run": cmd_run,
    "db": cmd_db,
    "scheduler": cmd_scheduler,
}


def main(argv: Optional[List[str]] = None) -> None:
    parser = build_parser()
    args = parser.parse_args(argv)

    # Configure logging
    logging.basicConfig(
        level=getattr(logging, args.log_level, logging.INFO),
        format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
        datefmt="%H:%M:%S",
    )

    handler = _COMMAND_MAP.get(args.command)
    if handler:
        try:
            handler(args)
        except KeyboardInterrupt:
            print("\nAborted.", file=sys.stderr)
            sys.exit(0)
        except Exception as exc:
            log.exception("Command failed: %s", exc)
            sys.exit(1)
    else:
        parser.print_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
