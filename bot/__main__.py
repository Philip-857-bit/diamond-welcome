"""
Entry point — ``python -m bot``.

Builds the Application, registers all handlers, and runs a Starlette webhook
server (uvicorn) so Render sees inbound HTTP traffic and keeps the container alive.
"""

import asyncio
import logging
from http import HTTPStatus

import uvicorn
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import PlainTextResponse, Response
from starlette.routing import Route

from telegram import Update
from telegram.ext import Application, CallbackQueryHandler, MessageHandler, filters
from telegram.request import HTTPXRequest

from bot.config import (
    BOT_TOKEN,
    PORT,
    RENDER_EXTERNAL_URL,
    WEBHOOK_PATH,
    WEBHOOK_SECRET,
    setup_logging,
)
from bot.error_handler import error_handler
from bot.handlers import button_handler, new_member_handler

logger = logging.getLogger(__name__)


async def health(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok", status_code=HTTPStatus.OK)


async def webhook(request: Request) -> Response:
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if header_secret != WEBHOOK_SECRET:
        return Response(status_code=HTTPStatus.FORBIDDEN)

    update = Update.de_json(data=await request.json(), bot=application.bot)
    await application.update_queue.put(update)
    return Response()


def build_application() -> Application:
    request_config = HTTPXRequest(
        connect_timeout=10.0,
        read_timeout=60.0,
        write_timeout=60.0,
        pool_timeout=10.0,
    )

    app = (
        Application.builder()
        .token(BOT_TOKEN)
        .updater(None)
        .request(request_config)
        .get_updates_request(HTTPXRequest(connect_timeout=10.0, read_timeout=30.0))
        .build()
    )

    app.add_handler(
        MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, new_member_handler)
    )
    app.add_handler(
        CallbackQueryHandler(button_handler, pattern=r"^verify:")
    )
    app.add_error_handler(error_handler)

    return app


application = build_application()


def main() -> None:
    setup_logging()

    starlette_app = Starlette(
        routes=[
            Route("/", health, methods=["GET"]),
            Route(f"/{WEBHOOK_PATH}", webhook, methods=["POST"]),
        ]
    )

    config = uvicorn.Config(
        app=starlette_app,
        host="0.0.0.0",
        port=PORT,
        log_level="info",
    )
    server = uvicorn.Server(config)

    async def _serve() -> None:
        await server.serve()

    async def run() -> None:
        webhook_url = f"{RENDER_EXTERNAL_URL}/{WEBHOOK_PATH}"
        logger.info("Setting webhook: %s", webhook_url)
        await application.bot.set_webhook(
            url=webhook_url,
            secret_token=WEBHOOK_SECRET,
            drop_pending_updates=True,
            allowed_updates=Update.ALL_TYPES,
        )

        async with application:
            await application.start()
            logger.info("Bot started — listening on port %s", PORT)
            await _serve()
            await application.stop()

        logger.info("Dropping webhook...")
        await application.bot.delete_webhook(drop_pending_updates=True)

    asyncio.run(run())


if __name__ == "__main__":
    main()
