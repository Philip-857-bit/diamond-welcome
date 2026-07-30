"""
Entry point — ``python -m bot``.

Builds the Application, registers all handlers, and starts polling.
"""

import logging

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters

from bot.config import BOT_TOKEN, setup_logging
from bot.error_handler import error_handler
from bot.handlers import button_handler, new_member_handler

logger = logging.getLogger(__name__)


def main() -> None:
    setup_logging()

    application = Application.builder().token(BOT_TOKEN).build()

    # F1.1: Listen for new members joining
    application.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler)
    )

    # F3: Handle CAPTCHA button presses (only "verify:…" callbacks)
    application.add_handler(
        CallbackQueryHandler(button_handler, pattern=r"^verify:")
    )

    # Global error handler (RetryAfter, etc.)
    application.add_error_handler(error_handler)

    logger.info("Bot starting...")
    application.run_polling(allowed_updates=Update.ALL_TYPES)


if __name__ == "__main__":
    main()
