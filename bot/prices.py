"""SOL/USD price feed via Binance public API with in-memory caching."""

from __future__ import annotations

import logging
import time
from decimal import Decimal
from typing import Any

import httpx

logger = logging.getLogger(__name__)

BINANCE_PRICE_URL = "https://api.binance.com/api/v3/ticker/price?symbol=SOLUSDT"
CACHE_TTL_SECONDS = 30.0

_cache: tuple[Decimal, float] | None = None


async def _fetch_sol_raw() -> Decimal | None:
    async with httpx.AsyncClient(timeout=httpx.Timeout(10.0)) as http:
        try:
            response = await http.get(BINANCE_PRICE_URL)
            response.raise_for_status()
            data: dict[str, Any] = response.json()
        except Exception:
            logger.exception("Binance SOL price fetch failed")
            return None

    try:
        return Decimal(str(data["price"]))
    except (KeyError, TypeError) as exc:
        logger.error("Unexpected Binance price response: %s", exc)
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
