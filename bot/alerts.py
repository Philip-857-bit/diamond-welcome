"""Durable event worker and animated Telegram buy-alert delivery."""

from __future__ import annotations

import asyncio
import html
import logging
import os
import time
from decimal import Decimal

from telegram import Bot
from telegram.error import BadRequest, NetworkError, RetryAfter

from bot.database import Database
from bot.prices import get_sol_price
from bot.solana import BuyAlert, parse_buys

logger = logging.getLogger(__name__)


class UncertainDeliveryError(RuntimeError):
    """A Telegram send may have completed and requires owner reconciliation."""


def _number(value: Decimal) -> str:
    if value == 0:
        return "0"
    absolute = abs(value)
    places = 2 if absolute >= 100 else 4 if absolute >= 1 else 8
    rendered = f"{value:,.{places}f}".rstrip("0").rstrip(".")
    return rendered


def _short(address: str) -> str:
    return f"{address[:5]}…{address[-5:]}"


def _usd_str(
    amount: Decimal, symbol: str | None, sol_price: Decimal | None
) -> str | None:
    if symbol == "SOL" and sol_price is not None and sol_price > 0:
        usd = amount * sol_price
        return f"${_number(usd)}"
    if symbol in {"USDC", "USDT"}:
        return f"${_number(amount)}"
    return None


def build_caption(alert: BuyAlert, sol_price: Decimal | None = None) -> str:
    name = html.escape(alert.token.name or alert.token.symbol or "Watched token")
    symbol = html.escape(alert.token.symbol or "TOKEN")
    payment = "Not detected"
    if alert.payment_amount is not None and alert.payment_symbol:
        native = f"{_number(alert.payment_amount)} {html.escape(alert.payment_symbol)}"
        usd = _usd_str(alert.payment_amount, alert.payment_symbol, sol_price)
        if usd:
            payment = f"{native} ({usd})"
        else:
            payment = native
    tx_url = f"https://solscan.io/tx/{html.escape(alert.signature, quote=True)}"
    return (
        "🦆🚀 <b>SolDucks Buy Alert!</b> 🚀🦆\n\n"
        f"💎 <b>{name} ({symbol})</b>\n"
        f"🪙 <b>Bought:</b> {_number(alert.token_amount)} {symbol}\n"
        f"💰 <b>Paid:</b> {payment}\n"
        f"👤 <b>Buyer:</b> <code>{html.escape(_short(alert.buyer))}</code>\n"
        f"🔑 <b>Mint:</b> <code>{html.escape(_short(alert.token.mint))}</code>\n\n"
        f'🔎 <a href="{tx_url}">View transaction on Solscan</a>\n'
        "🟢🟢🟢🦆🟢🟢🟢"
    )


class AlertWorker:
    def __init__(
        self,
        database: Database,
        bot: Bot,
        animation_path: str,
        poll_seconds: float,
        *,
        cleanup_seconds: float = 3600,
        retention_days: int = 30,
        dead_retention_days: int = 90,
    ) -> None:
        self.database = database
        self.bot = bot
        self.animation_path = animation_path
        self.poll_seconds = poll_seconds
        self.cleanup_seconds = cleanup_seconds
        self.retention_days = retention_days
        self.dead_retention_days = dead_retention_days
        self._stop = asyncio.Event()
        self._last_cleanup = time.monotonic()

    def stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        logger.info("SolDucks alert worker started")
        while not self._stop.is_set():
            event = None
            try:
                await self._cleanup_if_due()
                if await self.database.get_setting("alert_chat_id") is None:
                    await self._wait()
                    continue
                event = await self.database.claim_event()
                if event is None:
                    await self._wait()
                    continue
                tokens = {
                    token.mint: token for token in await self.database.list_tokens()
                }
                alerts = parse_buys(event.payload, tokens)
                if not alerts:
                    await self.database.mark_event(event.signature, "ignored")
                    continue
                message_id = await self._deliver_alerts(event.signature, alerts)
                await self.database.mark_event(
                    event.signature, "delivered", message_id=message_id
                )
            except asyncio.CancelledError:
                if event is not None:
                    await self.database.retry_event(
                        event.signature,
                        event.attempts,
                        "Worker stopped during delivery",
                    )
                raise
            except RetryAfter as exc:
                if event is not None:
                    retry_after = exc.retry_after
                    delay_seconds = (
                        retry_after.total_seconds()
                        if hasattr(retry_after, "total_seconds")
                        else float(retry_after)
                    )
                    try:
                        await self.database.defer_event(
                            event.signature,
                            max(1.0, delay_seconds) + 1.0,
                            str(exc),
                        )
                    except Exception:
                        logger.exception(
                            "Could not defer rate-limited event %s", event.signature
                        )
                    event = None
                await self._wait()
            except UncertainDeliveryError as exc:
                if event is not None:
                    logger.error(
                        "Alert delivery state is uncertain for %s: %s",
                        event.signature,
                        exc,
                    )
                    try:
                        await self.database.mark_event(
                            event.signature, "dead", error=str(exc)
                        )
                    except Exception:
                        logger.exception(
                            "Could not dead-letter uncertain event %s",
                            event.signature,
                        )
                    event = None
                await self._wait()
            except Exception as exc:
                if event is not None:
                    logger.exception("Alert delivery failed for %s", event.signature)
                    try:
                        await self.database.retry_event(
                            event.signature, event.attempts, str(exc)
                        )
                    except Exception:
                        logger.exception(
                            "Could not reschedule event %s", event.signature
                        )
                    event = None
                else:
                    logger.exception("Alert worker database operation failed")
                await self._wait()
        logger.info("SolDucks alert worker stopped")

    async def _wait(self) -> None:
        try:
            await asyncio.wait_for(self._stop.wait(), timeout=self.poll_seconds)
        except TimeoutError:
            pass

    async def _cleanup_if_due(self) -> None:
        now = time.monotonic()
        if now - self._last_cleanup < self.cleanup_seconds:
            return
        try:
            deleted = await self.database.cleanup_events(
                self.retention_days, self.dead_retention_days
            )
            if deleted:
                logger.info("Cleaned up %s expired chain events", deleted)
        except Exception:
            logger.exception("Chain-event cleanup failed")
        finally:
            self._last_cleanup = now

    async def _deliver_alerts(
        self, signature: str, alerts: list[BuyAlert]
    ) -> int | None:
        """Deliver only mint alerts not already completed for this transaction."""
        states = await self.database.alert_delivery_states(signature)
        message_id: int | None = None
        for alert in alerts:
            alert_key = (alert.token.mint, alert.buyer)
            state = states.get(alert_key)
            if state == "delivered":
                continue
            if state is not None:
                raise UncertainDeliveryError(
                    "Telegram delivery could not be confirmed. Use "
                    "/retryuncertain confirm to replay it if a possible duplicate "
                    "is acceptable."
                )
            reserved = await self.database.reserve_alert_delivery(
                signature, alert.token.mint, alert.buyer
            )
            if not reserved:
                raise UncertainDeliveryError(
                    "Telegram delivery was reserved concurrently and could not be confirmed."
                )
            try:
                message_id = await self._send(alert)
            except asyncio.CancelledError:
                await self._mark_uncertain(signature, alert)
                raise
            except RetryAfter:
                # Telegram rejected the request before delivery and supplied a
                # safe retry time, so this reservation can be released.
                await self.database.release_alert_delivery(
                    signature, alert.token.mint, alert.buyer
                )
                raise
            except BadRequest:
                # Telegram definitively rejected both the animation and any
                # attempted text fallback, so this reservation is safe to retry.
                await self.database.release_alert_delivery(
                    signature, alert.token.mint, alert.buyer
                )
                raise
            except NetworkError:
                await self._mark_uncertain(signature, alert)
                raise
            except Exception:
                await self.database.release_alert_delivery(
                    signature, alert.token.mint, alert.buyer
                )
                raise
            await self.database.mark_alert_delivered(
                signature, alert.token.mint, alert.buyer, message_id
            )
            states[alert_key] = "delivered"
        return message_id

    async def _mark_uncertain(self, signature: str, alert: BuyAlert) -> None:
        try:
            await asyncio.shield(
                self.database.mark_alert_uncertain(
                    signature, alert.token.mint, alert.buyer
                )
            )
        except Exception:
            # The committed 'sending' reservation is also treated as uncertain
            # on the next claim, so failure here never marks it delivered.
            logger.exception("Could not mark Telegram delivery as uncertain")

    async def _send(self, alert: BuyAlert) -> int:
        chat_value = await self.database.get_setting("alert_chat_id")
        if chat_value is None:
            raise RuntimeError("Alert chat is not configured")
        chat_id = int(chat_value)
        sol_price = await get_sol_price()
        caption = build_caption(alert, sol_price)
        cached_id = await self.database.get_setting("alert_animation_file_id")
        has_local_animation = os.path.isfile(self.animation_path)
        if not cached_id and not has_local_animation:
            message = await self.bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
            return message.message_id

        try:
            if cached_id:
                message = await self.bot.send_animation(
                    chat_id=chat_id,
                    animation=cached_id,
                    caption=caption,
                    parse_mode="HTML",
                )
            else:
                with open(self.animation_path, "rb") as animation:
                    message = await self.bot.send_animation(
                        chat_id=chat_id,
                        animation=animation,
                        caption=caption,
                        parse_mode="HTML",
                    )
                if message.animation:
                    try:
                        await self.database.set_setting(
                            "alert_animation_file_id", message.animation.file_id
                        )
                    except Exception:
                        # Caching is an optimization and must never turn a
                        # successful Telegram send into a retried duplicate.
                        logger.exception("Could not cache alert animation file ID")
        except BadRequest:
            if cached_id:
                # A stale file_id should not suppress the alert.
                try:
                    await self.database.set_setting("alert_animation_file_id", "")
                except Exception:
                    logger.exception("Could not clear stale alert animation file ID")
            message = await self.bot.send_message(
                chat_id=chat_id,
                text=caption,
                parse_mode="HTML",
                disable_web_page_preview=True,
            )
        except NetworkError:
            # A timeout/transport failure may occur after Telegram accepted the
            # animation, so a fallback here could create an immediate duplicate.
            raise
        return message.message_id
