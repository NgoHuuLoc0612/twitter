"""
CryptoAlerts — BTC/ETH (and any coin) price alerts.

Data Sources
------------
1. CoinGecko REST API (free tier, no key required)
   - Fetches current price, 24h change, volume, market cap
   - Used for the periodic price-check loop
2. Binance WebSocket Streams
   - wss://stream.binance.com:9443/stream?streams=btcusdt@miniTicker/ethusdt@miniTicker
   - Provides real-time sub-second price updates
   - Runs in a background thread

Alert Types
-----------
- price_above      : tweet when price exceeds threshold
- price_below      : tweet when price falls below threshold
- change_pct_up    : tweet on N% gain in 24h
- change_pct_down  : tweet on N% drop in 24h

Deduplication
-------------
Each alert type + threshold is rate-limited to once per `alert_cooldown_seconds`
(default 3600) stored in SQLite.
"""

from __future__ import annotations

import json
import logging
import threading
import time
from dataclasses import dataclass, field
from typing import Any, Callable, Dict, List, Optional, TYPE_CHECKING

import requests

if TYPE_CHECKING:
    from twitter.client import TwitterClient

log = logging.getLogger(__name__)

_COINGECKO_BASE = "https://api.coingecko.com/api/v3"
_BINANCE_WSS = "wss://stream.binance.com:9443/stream"

# CoinGecko IDs for common symbols
_SYMBOL_TO_CG_ID = {
    "BTC": "bitcoin",
    "ETH": "ethereum",
    "BNB": "binancecoin",
    "SOL": "solana",
    "ADA": "cardano",
    "DOT": "polkadot",
    "MATIC": "matic-network",
    "AVAX": "avalanche-2",
    "LINK": "chainlink",
    "UNI": "uniswap",
}

# Binance stream symbol mapping
_SYMBOL_TO_BINANCE = {
    "BTC": "btcusdt",
    "ETH": "ethusdt",
    "BNB": "bnbusdt",
    "SOL": "solusdt",
    "ADA": "adausdt",
}


@dataclass
class AlertRule:
    symbol: str
    alert_type: str          # 'price_above' | 'price_below' | 'change_pct_up' | 'change_pct_down'
    threshold: float
    tweet_template: str
    cooldown_seconds: int = 3600
    enabled: bool = True
    tags: List[str] = field(default_factory=list)


class CryptoAlerts:
    """
    Real-time and periodic crypto price alerts with Twitter integration.

    Parameters
    ----------
    client : TwitterClient
    symbols : list of str
        Symbols to track (e.g. ['BTC', 'ETH']).
    alert_rules : list of AlertRule
        Pre-configured alert rules.
    coingecko_api_key : str, optional
        CoinGecko Pro API key (optional — free tier used if not provided).
    poll_interval : int
        Seconds between CoinGecko REST polls.
    enable_binance_ws : bool
        Whether to open a Binance WebSocket for real-time updates.
    dry_run : bool
    """

    def __init__(
        self,
        client: "TwitterClient",
        symbols: Optional[List[str]] = None,
        alert_rules: Optional[List[AlertRule]] = None,
        coingecko_api_key: Optional[str] = None,
        poll_interval: int = 60,
        enable_binance_ws: bool = True,
        dry_run: bool = False,
    ):
        self.client = client
        self.symbols = symbols or ["BTC", "ETH"]
        self.alert_rules: List[AlertRule] = alert_rules or []
        self._cg_key = coingecko_api_key
        self.poll_interval = poll_interval
        self.enable_binance_ws = enable_binance_ws
        self.dry_run = dry_run
        self._db = client.db

        self._prices: Dict[str, Dict] = {}   # symbol → latest price data
        self._prices_lock = threading.Lock()

        self._ws_thread: Optional[threading.Thread] = None
        self._ws_running = threading.Event()
        self._poll_thread: Optional[threading.Thread] = None
        self._poll_running = threading.Event()

        self._session = requests.Session()
        self._session.headers["Accept"] = "application/json"
        if coingecko_api_key:
            self._session.headers["x-cg-pro-api-key"] = coingecko_api_key

    # ------------------------------------------------------------------
    # Alert rule management
    # ------------------------------------------------------------------

    def add_rule(self, rule: AlertRule) -> None:
        self.alert_rules.append(rule)
        log.info("Added alert rule: %s %s @ %.4f", rule.symbol, rule.alert_type, rule.threshold)

    def add_price_above(
        self,
        symbol: str,
        threshold: float,
        tweet_template: Optional[str] = None,
        cooldown: int = 3600,
        tags: Optional[List[str]] = None,
    ) -> None:
        tmpl = tweet_template or (
            f"🚀 #{symbol} just broke above ${threshold:,.0f}! "
            f"Current price: ${{price}} (+{{change_24h}}% 24h)\n"
            f"#Crypto #Bitcoin #Altcoins"
        )
        self.add_rule(AlertRule(symbol, "price_above", threshold, tmpl, cooldown, tags=tags or []))

    def add_price_below(
        self,
        symbol: str,
        threshold: float,
        tweet_template: Optional[str] = None,
        cooldown: int = 3600,
        tags: Optional[List[str]] = None,
    ) -> None:
        tmpl = tweet_template or (
            f"⚠️ #{symbol} dropped below ${threshold:,.0f}. "
            f"Current: ${{price}} ({{change_24h}}% 24h)\n"
            f"#Crypto #MarketAlert"
        )
        self.add_rule(AlertRule(symbol, "price_below", threshold, tmpl, cooldown, tags=tags or []))

    def add_change_pct_up(
        self,
        symbol: str,
        pct: float,
        tweet_template: Optional[str] = None,
        cooldown: int = 3600,
    ) -> None:
        tmpl = tweet_template or (
            f"📈 #{symbol} is up {{change_24h}}% in the last 24h!\n"
            f"Price: ${{price}} — Volume: ${{volume}}\n#Crypto"
        )
        self.add_rule(AlertRule(symbol, "change_pct_up", pct, tmpl, cooldown))

    def add_change_pct_down(
        self,
        symbol: str,
        pct: float,
        tweet_template: Optional[str] = None,
        cooldown: int = 3600,
    ) -> None:
        tmpl = tweet_template or (
            f"📉 #{symbol} is down {{change_24h}}% in the last 24h.\n"
            f"Price: ${{price}} — Volume: ${{volume}}\n#Crypto #Dip"
        )
        self.add_rule(AlertRule(symbol, "change_pct_down", pct, tmpl, cooldown))

    # ------------------------------------------------------------------
    # Start / stop background threads
    # ------------------------------------------------------------------

    def start(self) -> None:
        """Start the CoinGecko polling loop and (optionally) Binance WS."""
        self._poll_running.set()
        self._poll_thread = threading.Thread(
            target=self._poll_loop, name="CG-poll", daemon=True
        )
        self._poll_thread.start()
        log.info("CoinGecko poll loop started (interval=%ds)", self.poll_interval)

        if self.enable_binance_ws:
            self._ws_running.set()
            self._ws_thread = threading.Thread(
                target=self._binance_ws_loop, name="Binance-WS", daemon=True
            )
            self._ws_thread.start()
            log.info("Binance WebSocket thread started")

    def stop(self) -> None:
        self._poll_running.clear()
        self._ws_running.clear()
        if self._poll_thread:
            self._poll_thread.join(timeout=5)
        if self._ws_thread:
            self._ws_thread.join(timeout=5)
        self._session.close()
        log.info("CryptoAlerts stopped")

    # ------------------------------------------------------------------
    # Manual fetch (single shot)
    # ------------------------------------------------------------------

    def fetch_prices_coingecko(self) -> Dict[str, Dict]:
        """Fetch current prices for all tracked symbols from CoinGecko."""
        cg_ids = [_SYMBOL_TO_CG_ID.get(s, s.lower()) for s in self.symbols]
        params = {
            "ids": ",".join(cg_ids),
            "vs_currencies": "usd",
            "include_24hr_change": "true",
            "include_24hr_vol": "true",
            "include_market_cap": "true",
            "precision": "full",
        }
        try:
            resp = self._session.get(
                f"{_COINGECKO_BASE}/simple/price", params=params, timeout=15
            )
            resp.raise_for_status()
            data = resp.json()
        except Exception as exc:
            log.error("CoinGecko fetch failed: %s", exc)
            return {}

        result: Dict[str, Dict] = {}
        for sym in self.symbols:
            cg_id = _SYMBOL_TO_CG_ID.get(sym, sym.lower())
            entry = data.get(cg_id, {})
            if not entry:
                continue
            price_data = {
                "symbol": sym,
                "price_usd": entry.get("usd", 0.0),
                "change_24h": entry.get("usd_24h_change", 0.0),
                "volume_24h": entry.get("usd_24h_vol", 0.0),
                "market_cap": entry.get("usd_market_cap", 0.0),
                "source": "coingecko",
                "ts": time.time(),
            }
            result[sym] = price_data
            self._db.record_price(
                sym,
                price_data["price_usd"],
                price_data["change_24h"],
                price_data["volume_24h"],
                price_data["market_cap"],
                "coingecko",
            )
            log.debug("CoinGecko %s: $%.4f (%.2f%%)", sym, price_data["price_usd"], price_data["change_24h"])

        with self._prices_lock:
            self._prices.update(result)

        return result

    def get_latest_prices(self) -> Dict[str, Dict]:
        with self._prices_lock:
            return dict(self._prices)

    def get_price_history(self, symbol: str, hours: int = 24) -> List[Dict]:
        since = time.time() - hours * 3600
        rows = self._db.get_price_history(symbol, since)
        return [dict(r) for r in rows]

    # ------------------------------------------------------------------
    # Alert evaluation
    # ------------------------------------------------------------------

    def evaluate_alerts(self, prices: Optional[Dict[str, Dict]] = None) -> List[Dict]:
        """
        Evaluate all alert rules against current prices.
        Posts tweets for triggered alerts. Returns list of triggered alert dicts.
        """
        if prices is None:
            prices = self.get_latest_prices()

        triggered = []
        for rule in self.alert_rules:
            if not rule.enabled:
                continue
            price_data = prices.get(rule.symbol)
            if not price_data:
                continue

            price = price_data.get("price_usd", 0.0)
            change_24h = price_data.get("change_24h", 0.0)
            volume = price_data.get("volume_24h", 0.0)

            should_fire = False
            if rule.alert_type == "price_above" and price > rule.threshold:
                should_fire = True
            elif rule.alert_type == "price_below" and price < rule.threshold:
                should_fire = True
            elif rule.alert_type == "change_pct_up" and change_24h >= rule.threshold:
                should_fire = True
            elif rule.alert_type == "change_pct_down" and change_24h <= -abs(rule.threshold):
                should_fire = True

            if not should_fire:
                continue

            # Cooldown check
            if self._db.alert_sent_recently(
                rule.symbol, rule.alert_type, rule.threshold, rule.cooldown_seconds
            ):
                log.debug(
                    "Alert %s %s %.4f in cooldown — skipping",
                    rule.symbol, rule.alert_type, rule.threshold
                )
                continue

            # Render template
            from twitter.utils.helpers import format_price, format_pct, format_large_number
            tweet_text = rule.tweet_template.format(
                symbol=rule.symbol,
                price=format_price(price),
                change_24h=format_pct(change_24h),
                volume=format_large_number(volume),
                threshold=format_price(rule.threshold),
            )
            tweet_text = tweet_text[:280]

            alert_info: Dict[str, Any] = {
                "symbol": rule.symbol,
                "alert_type": rule.alert_type,
                "threshold": rule.threshold,
                "price": price,
                "change_24h": change_24h,
                "tweet_text": tweet_text,
                "tweet_id": None,
                "dry_run": self.dry_run,
            }

            if self.dry_run:
                log.info("[DRY-RUN] Would tweet alert: %s", tweet_text)
                self._db.record_alert_sent(
                    rule.symbol, rule.alert_type, rule.threshold, price, "dry_run"
                )
            else:
                try:
                    resp = self.client.post_tweet(tweet_text)
                    tid = resp.get("id")
                    alert_info["tweet_id"] = tid
                    self._db.record_alert_sent(
                        rule.symbol, rule.alert_type, rule.threshold, price, tid
                    )
                    log.info("✓ Crypto alert tweeted: %s (tweet=%s)", tweet_text[:60], tid)
                except Exception as exc:
                    alert_info["error"] = str(exc)
                    log.error("✗ Failed to tweet crypto alert: %s", exc)

            triggered.append(alert_info)

        return triggered

    # ------------------------------------------------------------------
    # Background loops
    # ------------------------------------------------------------------

    def _poll_loop(self) -> None:
        while self._poll_running.is_set():
            try:
                prices = self.fetch_prices_coingecko()
                if prices:
                    self.evaluate_alerts(prices)
            except Exception as exc:
                log.exception("CoinGecko poll loop error: %s", exc)
            # Sleep in small increments so stop() is responsive
            for _ in range(self.poll_interval):
                if not self._poll_running.is_set():
                    return
                time.sleep(1)

    def _binance_ws_loop(self) -> None:
        """Connect to Binance combined stream and update prices in real-time."""
        try:
            import websocket  # websocket-client
        except ImportError:
            log.warning(
                "websocket-client not installed. "
                "pip install websocket-client to enable Binance WS."
            )
            return

        streams = [
            f"{_SYMBOL_TO_BINANCE[s]}@miniTicker"
            for s in self.symbols
            if s in _SYMBOL_TO_BINANCE
        ]
        if not streams:
            log.warning("No Binance stream mappings for symbols: %s", self.symbols)
            return

        url = f"{_BINANCE_WSS}?streams=" + "/".join(streams)
        log.info("Connecting to Binance WS: %s", url)

        def on_message(ws: Any, message: str) -> None:
            try:
                data = json.loads(message)
                stream = data.get("stream", "")
                ticker = data.get("data", {})
                # Determine symbol
                binance_sym = stream.split("@")[0].upper()
                # Reverse-map btcusdt → BTC
                sym = next(
                    (k for k, v in _SYMBOL_TO_BINANCE.items() if v == binance_sym.lower()),
                    None,
                )
                if not sym:
                    return
                price = float(ticker.get("c", 0))
                if price <= 0:
                    return

                self._db.record_price(sym, price, None, None, None, "binance")
                with self._prices_lock:
                    if sym not in self._prices:
                        self._prices[sym] = {}
                    self._prices[sym]["price_usd"] = price
                    self._prices[sym]["source"] = "binance"
                    self._prices[sym]["ts"] = time.time()

                log.debug("Binance WS %s: $%.4f", sym, price)
            except Exception as exc:
                log.debug("Binance WS message parse error: %s", exc)

        def on_error(ws: Any, error: Any) -> None:
            log.error("Binance WS error: %s", error)

        def on_close(ws: Any, *args) -> None:
            log.info("Binance WS closed")

        def on_open(ws: Any) -> None:
            log.info("Binance WS connected")

        reconnect_delay = 5
        while self._ws_running.is_set():
            ws = websocket.WebSocketApp(
                url,
                on_message=on_message,
                on_error=on_error,
                on_close=on_close,
                on_open=on_open,
            )
            ws.run_forever(ping_interval=30, ping_timeout=10)
            if not self._ws_running.is_set():
                break
            log.info("Binance WS reconnecting in %ds…", reconnect_delay)
            time.sleep(reconnect_delay)
            reconnect_delay = min(reconnect_delay * 2, 60)
