"""PostgreSQL persistence for SolDucks operators, tokens, settings, and events."""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import asyncpg

EVENT_CLAIM_LEASE_SECONDS = 600


@dataclass(frozen=True, slots=True)
class WatchedToken:
    mint: str
    name: str | None
    symbol: str | None
    decimals: int
    added_by: int


@dataclass(frozen=True, slots=True)
class PendingEvent:
    signature: str
    payload: dict[str, Any]
    attempts: int


SCHEMA = """
CREATE TABLE IF NOT EXISTS operators (
    user_id BIGINT PRIMARY KEY,
    added_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS exempt_users (
    identifier TEXT PRIMARY KEY,
    added_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS watched_tokens (
    mint TEXT PRIMARY KEY,
    name TEXT,
    symbol TEXT,
    decimals SMALLINT NOT NULL
        CONSTRAINT watched_tokens_decimals_u8_check
        CHECK (decimals BETWEEN 0 AND 255),
    added_by BIGINT NOT NULL,
    created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

ALTER TABLE watched_tokens
    DROP CONSTRAINT IF EXISTS watched_tokens_decimals_check;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'watched_tokens'::regclass
          AND conname = 'watched_tokens_decimals_u8_check'
    ) THEN
        ALTER TABLE watched_tokens
            ADD CONSTRAINT watched_tokens_decimals_u8_check
            CHECK (decimals BETWEEN 0 AND 255);
    END IF;
END $$;

CREATE TABLE IF NOT EXISTS settings (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS chain_events (
    signature TEXT PRIMARY KEY,
    payload JSONB NOT NULL,
    status TEXT NOT NULL DEFAULT 'pending'
        CHECK (status IN ('pending', 'processing', 'delivered', 'ignored', 'failed', 'dead')),
    attempts INTEGER NOT NULL DEFAULT 0,
    next_attempt_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    last_error TEXT,
    telegram_message_id BIGINT,
    received_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS alert_deliveries (
    signature TEXT NOT NULL REFERENCES chain_events(signature) ON DELETE CASCADE,
    mint TEXT NOT NULL,
    buyer TEXT NOT NULL,
    status TEXT NOT NULL
        CONSTRAINT alert_deliveries_status_check
        CHECK (status IN ('sending', 'delivered', 'uncertain')),
    telegram_message_id BIGINT,
    delivered_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    PRIMARY KEY (signature, mint, buyer)
);

ALTER TABLE alert_deliveries
    ADD COLUMN IF NOT EXISTS buyer TEXT NOT NULL DEFAULT '';
ALTER TABLE alert_deliveries ALTER COLUMN buyer DROP DEFAULT;
ALTER TABLE alert_deliveries ALTER COLUMN telegram_message_id DROP NOT NULL;
ALTER TABLE alert_deliveries
    ADD COLUMN IF NOT EXISTS status TEXT NOT NULL DEFAULT 'delivered';
UPDATE alert_deliveries
    SET status = 'uncertain'
    WHERE telegram_message_id IS NULL AND status = 'delivered';
ALTER TABLE alert_deliveries ALTER COLUMN status DROP DEFAULT;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conrelid = 'alert_deliveries'::regclass
          AND conname = 'alert_deliveries_status_check'
    ) THEN
        ALTER TABLE alert_deliveries
            ADD CONSTRAINT alert_deliveries_status_check
            CHECK (status IN ('sending', 'delivered', 'uncertain'));
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (
        SELECT 1
        FROM pg_constraint AS constraint_row
        WHERE constraint_row.conrelid = 'alert_deliveries'::regclass
          AND constraint_row.contype = 'p'
          AND ARRAY(
              SELECT attribute_row.attname::text
              FROM unnest(constraint_row.conkey) WITH ORDINALITY
                   AS key_row(attnum, position)
              JOIN pg_attribute AS attribute_row
                ON attribute_row.attrelid = constraint_row.conrelid
               AND attribute_row.attnum = key_row.attnum
              ORDER BY key_row.position
          ) = ARRAY['signature', 'mint', 'buyer']::text[]
    ) THEN
        ALTER TABLE alert_deliveries
            DROP CONSTRAINT IF EXISTS alert_deliveries_pkey;
        ALTER TABLE alert_deliveries
            ADD CONSTRAINT alert_deliveries_pkey
            PRIMARY KEY (signature, mint, buyer);
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS chain_events_pending_idx
    ON chain_events (next_attempt_at, received_at)
    WHERE status IN ('pending', 'failed');
"""


class Database:
    """Small parameterized-query data layer."""

    def __init__(self, url: str) -> None:
        self._url = url
        self.pool: asyncpg.Pool | None = None

    async def connect(self) -> None:
        self.pool = await asyncpg.create_pool(self._url, min_size=1, max_size=5)
        async with self.pool.acquire() as conn:
            await conn.execute(SCHEMA)

    async def close(self) -> None:
        if self.pool:
            await self.pool.close()
            self.pool = None

    def _pool(self) -> asyncpg.Pool:
        if self.pool is None:
            raise RuntimeError("Database has not been connected")
        return self.pool

    async def is_operator(self, user_id: int) -> bool:
        value = await self._pool().fetchval(
            "SELECT EXISTS(SELECT 1 FROM operators WHERE user_id = $1)", user_id
        )
        return bool(value)

    async def add_operator(self, user_id: int, added_by: int) -> bool:
        result = await self._pool().execute(
            "INSERT INTO operators (user_id, added_by) VALUES ($1, $2) "
            "ON CONFLICT (user_id) DO NOTHING",
            user_id,
            added_by,
        )
        return result.endswith("1")

    async def remove_operator(self, user_id: int) -> bool:
        result = await self._pool().execute(
            "DELETE FROM operators WHERE user_id = $1", user_id
        )
        return result.endswith("1")

    async def list_operators(self) -> list[int]:
        rows = await self._pool().fetch(
            "SELECT user_id FROM operators ORDER BY user_id"
        )
        return [int(row["user_id"]) for row in rows]

    async def is_exempt_member(
        self, user_id: int, username: str | None, first_name: str | None
    ) -> bool:
        identifiers = [str(user_id)]
        if username:
            identifiers.append("@" + username.lower())
        if first_name:
            identifiers.append("name:" + first_name.strip().lower())
        value = await self._pool().fetchval(
            "SELECT EXISTS(SELECT 1 FROM exempt_users WHERE identifier = ANY($1::text[]))",
            identifiers,
        )
        return bool(value)

    async def add_exempt(self, identifier: str, added_by: int) -> bool:
        result = await self._pool().execute(
            "INSERT INTO exempt_users (identifier, added_by) VALUES ($1, $2) "
            "ON CONFLICT (identifier) DO NOTHING",
            identifier,
            added_by,
        )
        return result.endswith("1")

    async def remove_exempt(self, identifier: str) -> bool:
        result = await self._pool().execute(
            "DELETE FROM exempt_users WHERE identifier = $1", identifier
        )
        return result.endswith("1")

    async def list_exempt(self) -> list[str]:
        rows = await self._pool().fetch(
            "SELECT identifier FROM exempt_users ORDER BY identifier"
        )
        return [str(row["identifier"]) for row in rows]

    async def add_token(self, token: WatchedToken) -> bool:
        result = await self._pool().execute(
            "INSERT INTO watched_tokens (mint, name, symbol, decimals, added_by) "
            "VALUES ($1, $2, $3, $4, $5) ON CONFLICT (mint) DO NOTHING",
            token.mint,
            token.name,
            token.symbol,
            token.decimals,
            token.added_by,
        )
        return result.endswith("1")

    async def remove_token(self, mint: str) -> WatchedToken | None:
        row = await self._pool().fetchrow(
            "DELETE FROM watched_tokens WHERE mint = $1 "
            "RETURNING mint, name, symbol, decimals, added_by",
            mint,
        )
        return self._token(row) if row else None

    async def get_token(self, mint: str) -> WatchedToken | None:
        row = await self._pool().fetchrow(
            "SELECT mint, name, symbol, decimals, added_by FROM watched_tokens WHERE mint = $1",
            mint,
        )
        return self._token(row) if row else None

    async def list_tokens(self) -> list[WatchedToken]:
        rows = await self._pool().fetch(
            "SELECT mint, name, symbol, decimals, added_by "
            "FROM watched_tokens ORDER BY created_at, mint"
        )
        return [self._token(row) for row in rows]

    async def alert_delivery_states(self, signature: str) -> dict[tuple[str, str], str]:
        rows = await self._pool().fetch(
            "SELECT mint, buyer, status FROM alert_deliveries WHERE signature = $1",
            signature,
        )
        return {
            (str(row["mint"]), str(row["buyer"])): str(row["status"]) for row in rows
        }

    async def reserve_alert_delivery(
        self, signature: str, mint: str, buyer: str
    ) -> bool:
        """Reserve one external send before contacting Telegram."""
        result = await self._pool().execute(
            "INSERT INTO alert_deliveries (signature, mint, buyer, status) "
            "VALUES ($1, $2, $3, 'sending') "
            "ON CONFLICT (signature, mint, buyer) DO NOTHING",
            signature,
            mint,
            buyer,
        )
        return result.endswith("1")

    async def release_alert_delivery(
        self, signature: str, mint: str, buyer: str
    ) -> None:
        """Release a reservation only when no Telegram send completed."""
        await self._pool().execute(
            "DELETE FROM alert_deliveries "
            "WHERE signature = $1 AND mint = $2 AND buyer = $3 "
            "AND status = 'sending' AND telegram_message_id IS NULL",
            signature,
            mint,
            buyer,
        )

    async def mark_alert_uncertain(self, signature: str, mint: str, buyer: str) -> None:
        await self._pool().execute(
            "UPDATE alert_deliveries SET status = 'uncertain' "
            "WHERE signature = $1 AND mint = $2 AND buyer = $3 "
            "AND status <> 'delivered'",
            signature,
            mint,
            buyer,
        )

    async def mark_alert_delivered(
        self, signature: str, mint: str, buyer: str, message_id: int
    ) -> None:
        result = await self._pool().execute(
            "UPDATE alert_deliveries SET status = 'delivered', telegram_message_id = $4, "
            "delivered_at = NOW() "
            "WHERE signature = $1 AND mint = $2 AND buyer = $3",
            signature,
            mint,
            buyer,
            message_id,
        )
        if not result.endswith("1"):
            raise RuntimeError("Alert delivery reservation was lost")

    @staticmethod
    def _token(row: asyncpg.Record) -> WatchedToken:
        return WatchedToken(
            mint=row["mint"],
            name=row["name"],
            symbol=row["symbol"],
            decimals=int(row["decimals"]),
            added_by=int(row["added_by"]),
        )

    async def set_setting(self, key: str, value: str) -> None:
        await self._pool().execute(
            "INSERT INTO settings (key, value) VALUES ($1, $2) "
            "ON CONFLICT (key) DO UPDATE SET value = EXCLUDED.value, updated_at = NOW()",
            key,
            value,
        )

    async def get_setting(self, key: str) -> str | None:
        return await self._pool().fetchval(
            "SELECT value FROM settings WHERE key = $1", key
        )

    async def enqueue_events(self, events: list[dict[str, Any]]) -> int:
        valid = [
            (signature, json.dumps(event))
            for event in events
            if isinstance((signature := event.get("signature")), str)
            and signature
            and len(signature) <= 128
        ]
        if not valid:
            return 0
        signatures, payloads = zip(*valid, strict=True)
        rows = await self._pool().fetch(
            "INSERT INTO chain_events (signature, payload) "
            "SELECT signature, payload::jsonb "
            "FROM UNNEST($1::text[], $2::text[]) AS data(signature, payload) "
            "ON CONFLICT (signature) DO NOTHING RETURNING signature",
            list(signatures),
            list(payloads),
        )
        return len(rows)

    async def claim_event(self) -> PendingEvent | None:
        async with self._pool().acquire() as conn:
            async with conn.transaction():
                row = await conn.fetchrow(
                    "SELECT signature, payload, attempts FROM chain_events "
                    "WHERE (status IN ('pending', 'failed') AND next_attempt_at <= NOW()) "
                    "OR (status = 'processing' AND updated_at <= NOW() - "
                    "($1::double precision * INTERVAL '1 second')) "
                    "ORDER BY received_at FOR UPDATE SKIP LOCKED LIMIT 1",
                    EVENT_CLAIM_LEASE_SECONDS,
                )
                if row is None:
                    return None
                await conn.execute(
                    "UPDATE chain_events SET status = 'processing', attempts = attempts + 1, "
                    "updated_at = NOW() WHERE signature = $1",
                    row["signature"],
                )
                payload = row["payload"]
                if isinstance(payload, str):
                    payload = json.loads(payload)
                return PendingEvent(
                    signature=row["signature"],
                    payload=dict(payload),
                    attempts=int(row["attempts"]) + 1,
                )

    async def mark_event(
        self,
        signature: str,
        status: str,
        *,
        error: str | None = None,
        message_id: int | None = None,
    ) -> None:
        await self._pool().execute(
            "UPDATE chain_events SET status = $2, last_error = $3, "
            "telegram_message_id = COALESCE($4, telegram_message_id), updated_at = NOW() "
            "WHERE signature = $1",
            signature,
            status,
            error[:500] if error else None,
            message_id,
        )

    async def retry_event(self, signature: str, attempts: int, error: str) -> None:
        if attempts >= 10:
            await self.mark_event(signature, "dead", error=error)
            return
        delay_seconds = min(300, 2**attempts)
        await self._pool().execute(
            "UPDATE chain_events SET status = 'failed', last_error = $2, "
            "next_attempt_at = NOW() + ($3::double precision * INTERVAL '1 second'), updated_at = NOW() "
            "WHERE signature = $1",
            signature,
            error[:500],
            delay_seconds,
        )

    async def defer_event(
        self, signature: str, delay_seconds: float, error: str
    ) -> None:
        """Defer a rate-limited event without consuming a delivery attempt."""
        await self._pool().execute(
            "UPDATE chain_events SET status = 'failed', "
            "attempts = GREATEST(attempts - 1, 0), last_error = $2, "
            "next_attempt_at = NOW() + ($3::double precision * INTERVAL '1 second'), "
            "updated_at = NOW() WHERE signature = $1",
            signature,
            error[:500],
            delay_seconds,
        )

    async def requeue_dead_events(self, *, clear_uncertain: bool = False) -> int:
        async with self._pool().acquire() as conn:
            async with conn.transaction():
                if clear_uncertain:
                    await conn.execute(
                        "DELETE FROM alert_deliveries AS delivery "
                        "USING chain_events AS event "
                        "WHERE delivery.signature = event.signature "
                        "AND event.status = 'dead' "
                        "AND delivery.status <> 'delivered'"
                    )
                result = await conn.execute(
                    "UPDATE chain_events SET status = 'pending', attempts = 0, "
                    "next_attempt_at = NOW(), last_error = NULL, updated_at = NOW() "
                    "WHERE status = 'dead' "
                    "AND ($1::boolean OR NOT EXISTS ("
                    "SELECT 1 FROM alert_deliveries AS delivery "
                    "WHERE delivery.signature = chain_events.signature "
                    "AND delivery.status <> 'delivered'))",
                    clear_uncertain,
                )
        return int(result.rsplit(" ", 1)[-1])

    async def uncertain_delivery_count(self) -> int:
        value = await self._pool().fetchval(
            "SELECT COUNT(*) FROM alert_deliveries WHERE status <> 'delivered'"
        )
        return int(value)

    async def cleanup_events(
        self, retention_days: int, dead_retention_days: int
    ) -> int:
        result = await self._pool().execute(
            "DELETE FROM chain_events WHERE "
            "(status IN ('delivered', 'ignored') AND "
            " updated_at < NOW() - ($1::double precision * INTERVAL '1 day')) OR "
            "(status = 'dead' AND "
            " updated_at < NOW() - ($2::double precision * INTERVAL '1 day'))",
            retention_days,
            dead_retention_days,
        )
        return int(result.rsplit(" ", 1)[-1])

    async def event_counts(self) -> dict[str, int]:
        rows = await self._pool().fetch(
            "SELECT status, COUNT(*) AS count FROM chain_events GROUP BY status"
        )
        return {str(row["status"]): int(row["count"]) for row in rows}
