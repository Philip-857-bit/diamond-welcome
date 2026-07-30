"""
Global error handler for the Telegram application.

Catches RetryAfter (429) rate limits gracefully and logs everything else.
"""

import asyncio
import logging

from telegram.error import RetryAfter
from telegram.ext import ContextTypes

logger = logging.getLogger(__name__)


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log errors; sleep on rate-limit instead of crashing."""
    if isinstance(context.error, RetryAfter):
        retry_after = context.error.retry_after
        logger.warning("Rate limited — sleeping for %s seconds", retry_after)
        await asyncio.sleep(retry_after)
        return

    logger.error("Unhandled exception:", exc_info=context.error)
