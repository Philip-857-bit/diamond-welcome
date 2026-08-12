"""SOL/USD price feed with in-memory caching and provider fallbacks."""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

import httpx

logger = logging.getLogger(__name__)

CACHE_TTL_SECONDS = 30.0

_PROVIDERS: tuple[tuple[str, str], ...] = (
    ("coinbase", "https://api.coinbase.com/v2/prices/SOL-USD/spot"),
    ("kraken", "https://api.kraken.com/0/public/Ticker?pair=SOLUSD"),
    ("coingecko", "https://api.coingecko.com/api/v3/simple/price?ids=solana&vs_currencies=usd"),
)

_cache: tuple[Decimal, float] | None = None


def _parse_price(provider: str, data: Any) -> Decimal | None:
    try:
        if provider == "coinbase":
            return Decimal(str(data["data"]["amount"]))
        if provider == "kraken":
            return Decimal(str(data["result"]["SOLUSD"]["c"][0]))
        if provider == "coingecko":
            return Decimal(str(data["solana"]["usd"]))
    except (KeyError, TypeError, IndexError, ValueError):
        return None
    return None


async def _fetch_sol_raw() -> Decimal | None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as http:
        for provider, url in _PROVIDERS:
            try:
                response = await http.get(url)
                response.raise_for_status()
                price = _parse_price(provider, response.json())
            except Exception as exc:
                logger.warning("SOL price fetch failed for %s: %s", provider, exc)
                continue
            if price is not None and price > 0:
                return price
    return None


async def get_sol_price() -> Decimal | None:
    global _cache
    if _cache is not None:
        price, fetched_at = _cache
        if time.monotonic() - fetched_at < CACHE_TTL_SECONDS:
            return price
    price = await _fetch_sol_raw()
    if price is not None:
        _cache = (price, time.monotonic())
    return price
