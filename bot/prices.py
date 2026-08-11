"""SOL/USD price feed via Jupiter API with in-memory caching."""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

import httpx

logger = logging.getLogger(__name__)

JUPITER_PRICE_URL = "https://api.jup.ag/price/v2?ids=So11111111111111111111111111111111111111112"
CACHE_TTL_SECONDS = 30.0

_cache: tuple[Decimal, float] | None = None


async def _fetch_sol_raw() -> Decimal | None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as http:
        try:
            response = await http.get(JUPITER_PRICE_URL)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
        except Exception:
            logger.exception("Jupiter SOL price fetch failed")
            return None

    try:
        price_data = data["data"]["So11111111111111111111111111111111111111112"]
        return Decimal(str(price_data["price"]))
    except (KeyError, TypeError) as exc:
        logger.error("Unexpected Jupiter price response: %s", exc)
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
