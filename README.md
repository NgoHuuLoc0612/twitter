# twitter — Python Automation Library

A production-grade Python library for Twitter (X) automation with real API integrations, SQLite caching, WebSocket streams, and a full CLI.

## Features

| Module | Description | APIs Used |
|---|---|---|
| **AutoFollow** | Batch follow by keyword / list / seed account | Twitter API v2 |
| **QuoteTweet** | Auto quote-tweet with templates | Twitter API v2 |
| **CryptoAlerts** | BTC/ETH price & change alerts | CoinGecko REST + Binance WSS |
| **ProductHuntTracker** | Daily top launches | Product Hunt GraphQL API v2 |
| **HackerNewsTracker** | Hot stories | HN Firebase API + Algolia |
| **CVEAlerts** | High-severity security CVEs | NIST NVD API v2.0 |

All modules share:
- **SQLite caching** — full deduplication, request logging, price history
- **Rate-limit awareness** — parses Twitter headers, sleeps until reset
- **Batch processing** — configurable caps, pagination, chunked API calls
- **Dry-run mode** — simulate everything without posting
- **Task scheduler** — cron-style interval and daily-at scheduling

---

## Requirements

- Python ≥ 3.10
- Twitter Developer account with v2 API access (Essential or higher)
- `requests` and `websocket-client`

```bash
pip install requests websocket-client python-dotenv
```

---

## Installation

```bash
git clone <repo>
cd twitter_lib
pip install -e ".[all]"
```

---

## Configuration

Copy `.env.example` to `.env` and fill in your credentials:

```bash
cp .env.example .env
```

Required Twitter credentials (from [developer portal](https://developer.twitter.com/en/portal/dashboard)):

```
TWITTER_API_KEY=...
TWITTER_API_SECRET=...
TWITTER_ACCESS_TOKEN=...
TWITTER_ACCESS_TOKEN_SECRET=...
TWITTER_BEARER_TOKEN=...
```

Optional (but recommended):
```
PRODUCT_HUNT_TOKEN=...   # from producthunt.com/v2/oauth/applications
NVD_API_KEY=...          # from nvd.nist.gov/developers/request-an-api-key
```

---

## Python API

### Quick start — run everything once

```python
from twitter.config import Config
from twitter.bot import Bot

cfg = Config.from_env(".env")
bot = Bot(cfg)

# Run all modules once
summary = bot.run_all()
print(summary)

bot.close()
```

### Auto-Follow

```python
from twitter.config import Config
from twitter.bot import Bot

cfg = Config.from_env(".env")
bot = Bot(cfg)

# Follow users who tweet about "machine learning"
results = bot.run_auto_follow(
    keyword="machine learning -is:retweet",
    max_users=20,
    min_followers=500,
    max_followers=100_000,
    language="en",
)

# Follow members of a Twitter List
results = bot.run_follow_list(list_id="12345678", max_users=50)

# Follow followers of an account
results = bot.run_follow_seed("sama", max_users=30)

# Follow explicit usernames
bot.auto_follow.follow_by_usernames(["elonmusk", "sama", "karpathy"])

print(bot.auto_follow.stats())
bot.close()
```

### Quote Tweets

```python
# Quote tweets about AI — comments are real quotes fetched from Quotable.io
results = bot.run_quote_tweets(
    keyword="OpenAI GPT",
    max_quotes=5,
    topic="tech",         # fetches tech/technology-tagged quotes from Quotable.io
    min_likes=50,
    hashtags=["AI", "LLM"],
)

# Disable Quotable.io and use static templates instead
from twitter.modules.quote_tweet import QuoteTweet

qt = QuoteTweet(bot.client, daily_cap=20, use_quote_api=False)
results = qt.quote_by_keyword(
    "#Python developer",
    max_quotes=3,
    comment_callable=lambda tweet: f"Great Python content! 🐍 Check this out 👇",
)

# Quote a specific tweet
result = qt.quote_specific_tweet(
    tweet_id="1234567890",
    comment="This is an important thread 🧵 Worth reading.",
)
```

### Crypto Alerts — CoinGecko + Binance WebSocket

```python
from twitter.config import Config
from twitter.bot import Bot

cfg = Config.from_env(".env")
bot = Bot(cfg)

# Add alert rules
bot.add_btc_above_alert(price=100_000, cooldown=3600)    # tweet when BTC > $100k
bot.add_btc_below_alert(price=50_000, cooldown=3600)     # tweet when BTC < $50k
bot.add_eth_above_alert(price=5_000)
bot.add_btc_change_alert(pct=5.0, direction="up")        # tweet on 5% 24h gain
bot.add_eth_change_alert(pct=10.0, direction="down")     # tweet on 10% 24h drop

# Custom alert with full template control
from twitter.modules.crypto_alerts import AlertRule

bot.crypto_alerts.add_rule(AlertRule(
    symbol="SOL",
    alert_type="price_above",
    threshold=200.0,
    tweet_template="🚀 #SOL just crossed $200! Current: ${price} ({change_24h} 24h)\n#Solana #Crypto",
    cooldown_seconds=7200,
))

# Start background polling + Binance WS
bot.start()

# Or manually fetch and evaluate
prices = bot.run_crypto_fetch()
print(prices)

# Price history (from SQLite)
history = bot.crypto_alerts.get_price_history("BTC", hours=24)

bot.stop()
bot.close()
```

### Product Hunt Tracker

```python
from datetime import datetime, timezone, timedelta

# Fetch today's posts
posts = bot.product_hunt.fetch_posts(max_posts=50)

# Fetch last 7 days
posts = bot.product_hunt.fetch_posts_batch(days=7, min_votes=100)

# Fetch by topic
posts = bot.product_hunt.fetch_posts(topic="developer-tools", max_posts=20)

# Tweet top 3 posts
results = bot.run_product_hunt(max_tweets=3, min_votes=100)

# Fetch specific date
yesterday = datetime.now(timezone.utc) - timedelta(days=1)
posts = bot.product_hunt.fetch_posts(date=yesterday)

# Custom tweet template
bot.product_hunt.tweet_template = (
    "🚀 New launch: {name}\n"
    "{tagline}\n\n"
    "▲ {votes} upvotes on @ProductHunt\n"
    "{topics}\n"
    "{url}"
)
results = bot.run_product_hunt(max_tweets=5)
```

### Hacker News

```python
# Fetch top stories (score ≥ 200)
stories = bot.hacker_news.fetch_stories(category="top", limit=50, min_score=200)

# Fetch best, new, ask, show stories
stories = bot.hacker_news.fetch_stories(category="best", limit=20)
stories = bot.hacker_news.fetch_stories(category="ask")

# Search via Algolia
stories = bot.hacker_news.search_algolia(
    query="large language models",
    num_results=10,
    min_points=50,
    days_ago=3,
)

# Tweet hot stories
results = bot.run_hacker_news(max_tweets=3, min_score=300, category="top")

# Cached stories
cached = bot.hacker_news.get_cached_stories(min_score=100, tweeted=False)
```

### CVE Security Alerts

```python
# Fetch CVEs published in the last 24 hours
cves = bot.cve_alerts.fetch_recent_cves(hours=24, max_results=500)

# Fetch recently MODIFIED CVEs (score changes, new patches)
cves = bot.cve_alerts.fetch_recent_cves(hours=48, modified=True)

# Search by keyword
cves = bot.cve_alerts.search_cves(keyword="Apache Log4j")

# Search by CPE (specific product)
cves = bot.cve_alerts.search_cves(cpe_name="cpe:2.3:a:apache:log4j:*")

# Search by CWE
cves = bot.cve_alerts.search_cves(cwe_id="CWE-79")  # XSS

# Fetch specific CVE
cve = bot.cve_alerts.fetch_cve_by_id("CVE-2021-44228")

# Tweet critical + high CVEs from last 24h
results = bot.run_cve_alerts(hours=24, max_tweets=5, min_cvss=9.0)

# Filter by severity
bot.cve_alerts.severities = ["CRITICAL"]
bot.cve_alerts.keywords = ["remote code execution", "authentication bypass"]
results = bot.run_cve_alerts()

# Cached CVEs
cached = bot.cve_alerts.get_cached_cves(min_score=9.0, severity="CRITICAL")
```

### Run Forever (with scheduler)

```python
from twitter.config import Config
from twitter.bot import Bot
from twitter.modules.crypto_alerts import AlertRule

cfg = Config.from_env(".env")

# Pre-build crypto alert rules
rules = [
    AlertRule("BTC", "price_above", 100_000, "🚀 #BTC hit $100k! Price: ${price}\n#Bitcoin"),
    AlertRule("BTC", "price_below",  40_000, "⚠️ #BTC dropped below $40k: ${price}\n#Bitcoin"),
    AlertRule("ETH", "change_pct_up",   10.0, "📈 #ETH +{change_24h} in 24h! Price: ${price}"),
    AlertRule("ETH", "change_pct_down", 10.0, "📉 #ETH {change_24h} in 24h. Price: ${price}"),
]

bot = Bot(cfg, crypto_alert_rules=rules)

# Register custom tasks
bot.scheduler.add_interval(
    "quote_ai_news",
    lambda: bot.run_quote_tweets("AI news", max_quotes=2, topic="tech"),
    interval=7200,          # every 2 hours
    run_immediately=False,
)

bot.scheduler.add_interval(
    "follow_dev_keyword",
    lambda: bot.run_auto_follow("python developer", max_users=5),
    interval=21600,         # every 6 hours
    run_immediately=False,
)

bot.run_forever()           # blocks; Ctrl-C to stop
```

---

## CLI

```bash
# Show bot status
twitter-cli status

# Follow by keyword
twitter-cli follow keyword "bitcoin developer" --max 20 --min-followers 500

# Follow Twitter List members
twitter-cli follow list 123456789 --max 50

# Follow followers of a seed account
twitter-cli follow seed VitalikButerin --max 30

# Quote tweets
twitter-cli quote keyword "ChatGPT" --max 5 --topic tech --min-likes 20

# Crypto — fetch prices and evaluate alerts
twitter-cli crypto fetch

# Crypto — price history
twitter-cli crypto history BTC --hours 48

# Crypto — add an alert
twitter-cli crypto alert add BTC above 100000

# Product Hunt
twitter-cli producthunt fetch --days-ago 0 --max 30
twitter-cli producthunt tweet --max 3 --min-votes 100

# Hacker News
twitter-cli hackernews fetch --category top --min-score 200
twitter-cli hackernews search "Rust programming" --days 3
twitter-cli hackernews tweet --max 3 --min-score 300

# CVE Alerts
twitter-cli cve fetch --hours 24 --max 200
twitter-cli cve search --keyword "remote code execution" --severity CRITICAL
twitter-cli cve get CVE-2021-44228
twitter-cli cve tweet --max 5 --min-cvss 9.0

# Run all modules once
twitter-cli run all

# Run forever
twitter-cli run forever

# Database
twitter-cli db stats
twitter-cli db follows --limit 20
twitter-cli db tweets --limit 10
twitter-cli db requests --limit 50

# Dry run (no actual posting)
twitter-cli --dry-run cve tweet --max 5
twitter-cli --dry-run follow keyword "developer" --max 10
```

---

## Database Schema (SQLite)

| Table | Purpose |
|---|---|
| `followed_users` | Every follow ever made (dedup) |
| `posted_tweets` | Every tweet posted (dedup) |
| `crypto_prices` | Full price history (BTC/ETH/…) |
| `crypto_alerts_sent` | Alert cooldown tracking |
| `product_hunt_cache` | Fetched PH posts |
| `hn_cache` | Fetched HN stories |
| `cve_cache` | Fetched CVE records |
| `rate_limits` | Per-endpoint Twitter rate-limit state |
| `request_log` | Full HTTP request/response audit trail |
| `kv_store` | Generic TTL-aware key-value cache |

---

## Rate Limits Reference

| Resource | Limit |
|---|---|
| Twitter follows | 400/day, 15/15 min |
| Twitter tweets | 300/3h (write) |
| CoinGecko (free) | 30 req/min |
| Binance WebSocket | Real-time, no limit |
| NVD (no key) | 5 req/30s |
| NVD (with key) | 50 req/30s |
| Product Hunt | ~500 req/hour |
| HN Firebase | No enforced limit |
| HN Algolia | No enforced limit |

---

## Project Layout

```
twitter/
├── __init__.py          # Public API exports
├── client.py            # TwitterClient — OAuth 1.0a + v2 REST
├── bot.py               # Bot orchestrator (recommended entry point)
├── config.py            # Typed configuration + env/file loaders
├── cli.py               # twitter-cli entry point
├── cache/
│   └── db.py            # SQLite CacheDB (thread-safe)
├── modules/
│   ├── auto_follow.py   # Batch follow engine
│   ├── quote_tweet.py   # Quote-tweet engine
│   ├── crypto_alerts.py # CoinGecko + Binance WS alerts
│   ├── product_hunt.py  # Product Hunt GraphQL tracker
│   ├── hacker_news.py   # HN Firebase + Algolia tracker
│   └── cve_alerts.py    # NIST NVD CVE alerts
└── utils/
    ├── rate_limiter.py  # Twitter rate-limit tracking
    ├── scheduler.py     # Cron-style task scheduler
    └── helpers.py       # Format/truncate/retry utilities
```
