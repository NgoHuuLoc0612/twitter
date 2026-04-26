"""
config.py — typed configuration for the twitter library.

Supports loading from:
  - Environment variables (TWITTER_API_KEY, etc.)
  - A .env file (via python-dotenv if installed)
  - A TOML file (Python 3.11+ tomllib or tomli backport)
  - A JSON file
  - Direct instantiation

Environment variable names
--------------------------
TWITTER_API_KEY
TWITTER_API_SECRET
TWITTER_ACCESS_TOKEN
TWITTER_ACCESS_TOKEN_SECRET
TWITTER_BEARER_TOKEN
TWITTER_DB_PATH               (default: twitter_cache.db)
TWITTER_DRY_RUN               (default: false)
TWITTER_LOG_LEVEL             (default: INFO)

PRODUCT_HUNT_TOKEN
NVD_API_KEY
COINGECKO_API_KEY

AUTO_FOLLOW_DAILY_CAP         (default: 50)
AUTO_FOLLOW_DELAY             (default: 65)
QUOTE_TWEET_DAILY_CAP         (default: 50)
CRYPTO_POLL_INTERVAL          (default: 60)
CRYPTO_ENABLE_BINANCE_WS      (default: true)
HN_MIN_SCORE                  (default: 100)
CVE_MIN_CVSS                  (default: 7.0)
CVE_SEVERITIES                (default: CRITICAL,HIGH)
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import List, Optional

log = logging.getLogger(__name__)


def _env(key: str, default: str = "") -> str:
    return os.environ.get(key, default).strip()


def _env_bool(key: str, default: bool = False) -> bool:
    val = _env(key)
    if not val:
        return default
    return val.lower() in ("1", "true", "yes", "on")


def _env_int(key: str, default: int = 0) -> int:
    val = _env(key)
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def _env_float(key: str, default: float = 0.0) -> float:
    val = _env(key)
    try:
        return float(val)
    except (ValueError, TypeError):
        return default


def _env_list(key: str, default: Optional[List[str]] = None, sep: str = ",") -> List[str]:
    val = _env(key)
    if not val:
        return default or []
    return [v.strip() for v in val.split(sep) if v.strip()]


# ---------------------------------------------------------------------------
# Config dataclasses
# ---------------------------------------------------------------------------

@dataclass
class TwitterCredentials:
    api_key: str = ""
    api_secret: str = ""
    access_token: str = ""
    access_token_secret: str = ""
    bearer_token: str = ""

    def validate(self) -> None:
        missing = [
            f for f in ("api_key", "api_secret", "access_token",
                        "access_token_secret", "bearer_token")
            if not getattr(self, f)
        ]
        if missing:
            raise ValueError(
                f"Missing Twitter credentials: {', '.join(missing)}. "
                "Set the corresponding TWITTER_* environment variables."
            )


@dataclass
class AutoFollowConfig:
    daily_cap: int = 50
    inter_follow_delay: float = 65.0
    dry_run: bool = False


@dataclass
class QuoteTweetConfig:
    daily_cap: int = 50
    inter_quote_delay: float = 5.0
    dry_run: bool = False
    use_quote_api: bool = True  # fetch real quotes from Quotable.io


@dataclass
class CryptoConfig:
    symbols: List[str] = field(default_factory=lambda: ["BTC", "ETH"])
    poll_interval: int = 60
    enable_binance_ws: bool = True
    coingecko_api_key: str = ""
    dry_run: bool = False


@dataclass
class ProductHuntConfig:
    ph_token: str = ""
    min_votes: int = 50
    dry_run: bool = False


@dataclass
class HackerNewsConfig:
    min_score: int = 100
    dry_run: bool = False


@dataclass
class CVEConfig:
    nvd_api_key: str = ""
    min_cvss_score: float = 7.0
    severities: List[str] = field(default_factory=lambda: ["CRITICAL", "HIGH"])
    keywords: List[str] = field(default_factory=list)
    dry_run: bool = False
    request_delay: float = 6.5


@dataclass
class Config:
    """Top-level library configuration."""

    credentials: TwitterCredentials = field(default_factory=TwitterCredentials)
    db_path: str = "twitter_cache.db"
    log_level: str = "INFO"
    dry_run: bool = False

    auto_follow: AutoFollowConfig = field(default_factory=AutoFollowConfig)
    quote_tweet: QuoteTweetConfig = field(default_factory=QuoteTweetConfig)
    crypto: CryptoConfig = field(default_factory=CryptoConfig)
    product_hunt: ProductHuntConfig = field(default_factory=ProductHuntConfig)
    hacker_news: HackerNewsConfig = field(default_factory=HackerNewsConfig)
    cve: CVEConfig = field(default_factory=CVEConfig)

    # ------------------------------------------------------------------
    # Factory: load from environment
    # ------------------------------------------------------------------

    @classmethod
    def from_env(cls, dotenv_path: Optional[str] = None) -> "Config":
        """
        Load configuration from environment variables.
        Optionally reads a .env file first (requires python-dotenv).
        """
        if dotenv_path:
            _load_dotenv(dotenv_path)

        global_dry = _env_bool("TWITTER_DRY_RUN", False)

        creds = TwitterCredentials(
            api_key=_env("TWITTER_API_KEY"),
            api_secret=_env("TWITTER_API_SECRET"),
            access_token=_env("TWITTER_ACCESS_TOKEN"),
            access_token_secret=_env("TWITTER_ACCESS_TOKEN_SECRET"),
            bearer_token=_env("TWITTER_BEARER_TOKEN"),
        )

        follow = AutoFollowConfig(
            daily_cap=_env_int("AUTO_FOLLOW_DAILY_CAP", 50),
            inter_follow_delay=_env_float("AUTO_FOLLOW_DELAY", 65.0),
            dry_run=global_dry,
        )

        qt = QuoteTweetConfig(
            daily_cap=_env_int("QUOTE_TWEET_DAILY_CAP", 50),
            dry_run=global_dry,
            use_quote_api=_env_bool("QUOTE_TWEET_USE_QUOTE_API", True),
        )

        crypto = CryptoConfig(
            symbols=_env_list("CRYPTO_SYMBOLS", ["BTC", "ETH"]),
            poll_interval=_env_int("CRYPTO_POLL_INTERVAL", 60),
            enable_binance_ws=_env_bool("CRYPTO_ENABLE_BINANCE_WS", True),
            coingecko_api_key=_env("COINGECKO_API_KEY"),
            dry_run=global_dry,
        )

        ph = ProductHuntConfig(
            ph_token=_env("PRODUCT_HUNT_TOKEN"),
            min_votes=_env_int("PH_MIN_VOTES", 50),
            dry_run=global_dry,
        )

        hn = HackerNewsConfig(
            min_score=_env_int("HN_MIN_SCORE", 100),
            dry_run=global_dry,
        )

        cve = CVEConfig(
            nvd_api_key=_env("NVD_API_KEY"),
            min_cvss_score=_env_float("CVE_MIN_CVSS", 7.0),
            severities=_env_list("CVE_SEVERITIES", ["CRITICAL", "HIGH"]),
            keywords=_env_list("CVE_KEYWORDS", []),
            dry_run=global_dry,
            request_delay=_env_float("NVD_REQUEST_DELAY", 6.5),
        )

        return cls(
            credentials=creds,
            db_path=_env("TWITTER_DB_PATH", "twitter_cache.db"),
            log_level=_env("TWITTER_LOG_LEVEL", "INFO"),
            dry_run=global_dry,
            auto_follow=follow,
            quote_tweet=qt,
            crypto=crypto,
            product_hunt=ph,
            hacker_news=hn,
            cve=cve,
        )

    @classmethod
    def from_toml(cls, path: str) -> "Config":
        """Load config from a TOML file."""
        try:
            import tomllib  # Python 3.11+
        except ImportError:
            try:
                import tomli as tomllib  # type: ignore[no-reuse-of-ignored-type]
            except ImportError:
                raise ImportError(
                    "Install tomli for TOML support: pip install tomli"
                )

        with open(path, "rb") as f:
            data = tomllib.load(f)

        return cls._from_dict(data)

    @classmethod
    def from_json(cls, path: str) -> "Config":
        """Load config from a JSON file."""
        with open(path) as f:
            data = json.load(f)
        return cls._from_dict(data)

    @classmethod
    def _from_dict(cls, d: dict) -> "Config":
        tw = d.get("twitter", {})
        creds = TwitterCredentials(**{k: tw.get(k, "") for k in TwitterCredentials.__dataclass_fields__})

        return cls(
            credentials=creds,
            db_path=d.get("db_path", "twitter_cache.db"),
            log_level=d.get("log_level", "INFO"),
            dry_run=d.get("dry_run", False),
            auto_follow=AutoFollowConfig(**d.get("auto_follow", {})),
            quote_tweet=QuoteTweetConfig(**d.get("quote_tweet", {})),
            crypto=CryptoConfig(**d.get("crypto", {})),
            product_hunt=ProductHuntConfig(**d.get("product_hunt", {})),
            hacker_news=HackerNewsConfig(**d.get("hacker_news", {})),
            cve=CVEConfig(**d.get("cve", {})),
        )

    # ------------------------------------------------------------------
    # Setup helpers
    # ------------------------------------------------------------------

    def configure_logging(self) -> None:
        level = getattr(logging, self.log_level.upper(), logging.INFO)
        logging.basicConfig(
            level=level,
            format="%(asctime)s [%(levelname)s] %(name)s — %(message)s",
            datefmt="%Y-%m-%d %H:%M:%S",
        )
        log.debug("Logging configured at level %s", self.log_level)

    def validate(self) -> None:
        self.credentials.validate()

    def dump(self) -> str:
        """Return a JSON representation (masks secrets)."""
        import dataclasses
        d = dataclasses.asdict(self)
        creds = d.get("credentials", {})
        for k in creds:
            if creds[k]:
                creds[k] = creds[k][:4] + "****"
        return json.dumps(d, indent=2)


# ---------------------------------------------------------------------------
# .env loader (optional dependency)
# ---------------------------------------------------------------------------

def _load_dotenv(path: str) -> None:
    p = Path(path)
    if not p.exists():
        log.debug(".env file not found at %s — skipping", path)
        return
    try:
        from dotenv import load_dotenv  # type: ignore
        load_dotenv(dotenv_path=path, override=False)
        log.info("Loaded .env from %s", path)
    except ImportError:
        # Manual simple parser
        log.debug("python-dotenv not installed — parsing .env manually")
        with open(path) as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, _, v = line.partition("=")
                k = k.strip()
                v = v.strip().strip('"').strip("'")
                if k and k not in os.environ:
                    os.environ[k] = v
