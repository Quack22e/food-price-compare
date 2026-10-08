"""Runtime configuration for the companion scraper service.

Every knob can be overridden with an environment variable so operators can
tighten politeness limits without editing code.
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from pathlib import Path

APP_ROOT = Path(__file__).resolve().parent.parent

# Public consumer sites. These are the only hosts the service will ever fetch
# (plus test overrides below), which also prevents the API from being used as
# an open proxy / SSRF vector.
DEFAULT_PLATFORM_BASES = {
    "doordash": "https://www.doordash.com",
    "ubereats": "https://www.ubereats.com",
    "grubhub": "https://www.grubhub.com",
}


def _env_float(name: str, default: float) -> float:
    try:
        return float(os.environ.get(name, default))
    except ValueError:
        return default


def _env_int(name: str, default: int) -> int:
    try:
        return int(os.environ.get(name, default))
    except ValueError:
        return default


@dataclass
class Config:
    # Truthful identification. robots.txt rules are evaluated for this token
    # (falling back to the "*" group).
    user_agent: str = os.environ.get(
        "FPC_USER_AGENT",
        "FoodPriceCompare/1.0 (personal menu-price comparison; honors robots.txt)",
    )
    robots_token: str = "FoodPriceCompare"
    # Politeness: minimum seconds between requests to the same host. A larger
    # robots.txt Crawl-delay always wins.
    min_interval_s: float = _env_float("FPC_MIN_INTERVAL", 3.0)
    concurrency_per_platform: int = max(1, min(4, _env_int("FPC_CONCURRENCY", 2)))
    timeout_s: float = _env_float("FPC_TIMEOUT", 25.0)
    max_retries: int = max(0, min(5, _env_int("FPC_MAX_RETRIES", 3)))
    backoff_base_s: float = _env_float("FPC_BACKOFF_BASE", 2.0)
    backoff_cap_s: float = _env_float("FPC_BACKOFF_CAP", 60.0)
    # A Retry-After longer than this stops the adapter instead of sleeping.
    max_retry_after_s: float = _env_float("FPC_MAX_RETRY_AFTER", 300.0)
    # Consecutive blocked/failed store fetches before an adapter is stopped.
    breaker_threshold: int = max(1, _env_int("FPC_BREAKER", 4))
    cache_ttl_page_s: int = _env_int("FPC_CACHE_TTL_PAGE", 6 * 3600)
    cache_ttl_sitemap_s: int = _env_int("FPC_CACHE_TTL_SITEMAP", 24 * 3600)
    cache_ttl_robots_s: int = _env_int("FPC_CACHE_TTL_ROBOTS", 24 * 3600)
    # Resource bounds for one run of a job (resume continues where it stopped).
    max_stores_cap: int = _env_int("FPC_MAX_STORES_CAP", 500)
    max_sitemaps_per_run: int = _env_int("FPC_MAX_SITEMAPS_PER_RUN", 60)
    max_sitemap_bytes: int = 50 * 1024 * 1024  # sitemaps.org protocol limit
    max_page_bytes: int = 15 * 1024 * 1024
    max_chain_pages: int = 5
    data_dir: Path = Path(os.environ.get("FPC_DATA_DIR", str(APP_ROOT / ".fpc-data")))
    platform_bases: dict = field(default_factory=lambda: dict(DEFAULT_PLATFORM_BASES))
    # Hosts a platform serves its robots.txt-declared sitemaps from. Only
    # sitemap fetches may use them. Observed live (2026-10-08): DoorDash's
    # declared sitemaps redirect to https://cdn.doordash.com/sitemaps/...
    sitemap_hosts: dict = field(default_factory=lambda: {"doordash": ["cdn.doordash.com"]})
    # Only the test-suite sets this; it lets plain-http local fixtures stand in
    # for the real hosts.
    test_mode: bool = False

    @classmethod
    def from_env(cls) -> "Config":
        cfg = cls()
        override = os.environ.get("FPC_TEST_BASES")
        if override:
            cfg.platform_bases.update(json.loads(override))
            cfg.test_mode = True
        return cfg

    @property
    def allowed_hosts(self) -> set[str]:
        from urllib.parse import urlsplit

        return {urlsplit(b).netloc.lower() for b in self.platform_bases.values()}

    @property
    def sitemap_only_hosts(self) -> set[str]:
        return {h.lower() for hosts in self.sitemap_hosts.values() for h in hosts}
