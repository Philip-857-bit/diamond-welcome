"""
Entry point — ``python -m bot``.

Builds the Application, registers all handlers, and runs a Starlette webhook
server (uvicorn) so Render sees inbound HTTP traffic and keeps the container alive.
"""

import asyncio
import json
import logging
import secrets
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
    ALERT_ANIMATION_PATH,
    ALERT_CHAT_ID,
    ALERT_WORKER_POLL_SECONDS,
    BOT_TOKEN,
    DATABASE_URL,
    DEAD_EVENT_RETENTION_DAYS,
    EVENT_CLEANUP_SECONDS,
    EVENT_RETENTION_DAYS,
    HELIUS_API_KEY,
    HELIUS_MAX_MONITORED_ADDRESSES,
    HELIUS_MAX_PAYLOAD_BYTES,
    HELIUS_WEBHOOK_PATH,
    HELIUS_WEBHOOK_SECRET,
    PORT,
    RENDER_EXTERNAL_URL,
    TOKEN_ACCOUNT_REFRESH_SECONDS,
    WEBHOOK_PATH,
    WEBHOOK_SECRET,
    setup_logging,
)
from bot.alerts import AlertWorker
from bot.commands import CommandRegistry, register_command_handlers
from bot.database import Database
from bot.error_handler import error_handler
from bot.handlers import button_handler, new_member_handler
from bot.solana import HeliusClient, TokenAccountRefresher, WatchlistService

logger = logging.getLogger(__name__)
HELIUS_ACK_TIMEOUT_SECONDS = 0.8


async def health(_: Request) -> PlainTextResponse:
    return PlainTextResponse("ok", status_code=HTTPStatus.OK)


async def webhook(request: Request) -> Response:
    header_secret = request.headers.get("X-Telegram-Bot-Api-Secret-Token")
    if not header_secret or not secrets.compare_digest(header_secret, WEBHOOK_SECRET):
        return Response(status_code=HTTPStatus.FORBIDDEN)

    update = Update.de_json(data=await request.json(), bot=application.bot)
    await application.update_queue.put(update)
    return Response()


async def helius_webhook(request: Request) -> Response:
    supplied = request.headers.get("Authorization", "")
    if not secrets.compare_digest(supplied, HELIUS_WEBHOOK_SECRET):
        return Response(status_code=HTTPStatus.FORBIDDEN)
    content_type = request.headers.get("Content-Type", "").lower()
    if not content_type.startswith("application/json"):
        return Response(status_code=HTTPStatus.UNSUPPORTED_MEDIA_TYPE)
    declared_length = request.headers.get("Content-Length")
    if declared_length:
        try:
            if int(declared_length) > HELIUS_MAX_PAYLOAD_BYTES:
                return Response(status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
        except ValueError:
            return Response(status_code=HTTPStatus.BAD_REQUEST)

    try:
        async with asyncio.timeout(HELIUS_ACK_TIMEOUT_SECONDS):
            body = bytearray()
            async for chunk in request.stream():
                body.extend(chunk)
                if len(body) > HELIUS_MAX_PAYLOAD_BYTES:
                    return Response(status_code=HTTPStatus.REQUEST_ENTITY_TOO_LARGE)
            try:
                payload = json.loads(body)
            except (json.JSONDecodeError, UnicodeDecodeError):
                return Response(status_code=HTTPStatus.BAD_REQUEST)
            events = payload if isinstance(payload, list) else [payload]
            if (
                not events
                or len(events) > 1000
                or not all(isinstance(item, dict) for item in events)
            ):
                return Response(status_code=HTTPStatus.BAD_REQUEST)

            database: Database | None = application.bot_data.get("database")
            if database is None:
                return Response(status_code=HTTPStatus.SERVICE_UNAVAILABLE)
            await database.enqueue_events(events)
            refresher: TokenAccountRefresher | None = application.bot_data.get(
                "token_account_refresher"
            )
            if refresher is not None:
                refresher.observe(events)
    except TimeoutError:
        logger.warning("Helius webhook persistence exceeded acknowledgement deadline")
        return Response(status_code=HTTPStatus.SERVICE_UNAVAILABLE)
    except Exception:
        logger.exception("Could not persist incoming Helius events")
        return Response(status_code=HTTPStatus.SERVICE_UNAVAILABLE)
    return Response(status_code=HTTPStatus.OK)


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
    app.add_handler(CallbackQueryHandler(button_handler, pattern=r"^verify:"))
    register_command_handlers(app)
    app.add_error_handler(error_handler)

    return app


application = build_application()


def main() -> None:
    setup_logging()

    if HELIUS_WEBHOOK_PATH == WEBHOOK_PATH.strip("/"):
        raise RuntimeError("HELIUS_WEBHOOK_PATH must differ from WEBHOOK_PATH")

    starlette_app = Starlette(
        routes=[
            Route("/", health, methods=["GET"]),
            Route(f"/{WEBHOOK_PATH}", webhook, methods=["POST"]),
            Route(f"/{HELIUS_WEBHOOK_PATH}", helius_webhook, methods=["POST"]),
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
        database = Database(DATABASE_URL)
        helius = HeliusClient(
            HELIUS_API_KEY,
            HELIUS_WEBHOOK_SECRET,
            RENDER_EXTERNAL_URL,
            HELIUS_WEBHOOK_PATH,
            HELIUS_MAX_MONITORED_ADDRESSES,
        )
        worker: AlertWorker | None = None
        worker_task: asyncio.Task[None] | None = None
        refresher: TokenAccountRefresher | None = None
        refresher_task: asyncio.Task[None] | None = None
        try:
            await database.connect()
            if (
                ALERT_CHAT_ID is not None
                and await database.get_setting("alert_chat_id") is None
            ):
                await database.set_setting("alert_chat_id", str(ALERT_CHAT_ID))
            watchlist = WatchlistService(database, helius)
            registry = CommandRegistry(application.bot, database)
            application.bot_data.update(
                database=database,
                helius=helius,
                watchlist=watchlist,
                command_registry=registry,
            )

            webhook_url = f"{RENDER_EXTERNAL_URL}/{WEBHOOK_PATH}"
            logger.info("Setting webhook: %s", webhook_url)
            async with application:
                await application.bot.set_webhook(
                    url=webhook_url,
                    secret_token=WEBHOOK_SECRET,
                    drop_pending_updates=True,
                    allowed_updates=Update.ALL_TYPES,
                )
                await application.start()
                try:
                    try:
                        await registry.register_all()
                    except Exception:
                        logger.exception("Initial Telegram command registration failed")
                    worker = AlertWorker(
                        database,
                        application.bot,
                        ALERT_ANIMATION_PATH,
                        ALERT_WORKER_POLL_SECONDS,
                        cleanup_seconds=EVENT_CLEANUP_SECONDS,
                        retention_days=EVENT_RETENTION_DAYS,
                        dead_retention_days=DEAD_EVENT_RETENTION_DAYS,
                    )
                    refresher = TokenAccountRefresher(
                        watchlist, TOKEN_ACCOUNT_REFRESH_SECONDS
                    )
                    application.bot_data["token_account_refresher"] = refresher
                    worker_task = asyncio.create_task(
                        worker.run(), name="solducks-alert-worker"
                    )
                    refresher_task = asyncio.create_task(
                        refresher.run(), name="solducks-token-account-refresher"
                    )
                    logger.info("SolDucks started — listening on port %s", PORT)
                    await _serve()
                finally:
                    if worker:
                        worker.stop()
                    if refresher:
                        refresher.stop()
                    application.bot_data.pop("token_account_refresher", None)
                    if worker_task:
                        await asyncio.gather(worker_task, return_exceptions=True)
                    if refresher_task:
                        await asyncio.gather(refresher_task, return_exceptions=True)
                    if application.running:
                        await application.stop()
        finally:
            if worker_task and not worker_task.done():
                if worker:
                    worker.stop()
                worker_task.cancel()
                await asyncio.gather(worker_task, return_exceptions=True)
            if refresher_task and not refresher_task.done():
                if refresher:
                    refresher.stop()
                refresher_task.cancel()
                await asyncio.gather(refresher_task, return_exceptions=True)
            await helius.close()
            await database.close()

    asyncio.run(run())


if __name__ == "__main__":
    main()
