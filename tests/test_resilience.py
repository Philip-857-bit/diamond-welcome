import asyncio
import unittest
from contextlib import asynccontextmanager
from unittest.mock import patch

import httpx
from telegram.error import BadRequest, RetryAfter, TimedOut

from bot.alerts import AlertWorker
from bot.database import SCHEMA, Database, WatchedToken
from bot.solana import (
    BuyAlert,
    HeliusClient,
    HeliusError,
    TokenAccountRefresher,
    WatchlistService,
    extract_token_accounts,
)


MINT = "So11111111111111111111111111111111111111112"
ATA_ONE = "7YttLkHDoVGPnXeoBHb7XgCwgftUR9akTiGAZCnZBq2i"
ATA_TWO = "9xQeWvG816bUx9EPfEZKj4qNQwTPVDGMuQeN9pL9dS7p"


class SyncDatabase:
    def __init__(self) -> None:
        self.settings = {
            "helius_webhook_id": "webhook-id",
            "helius_coverage_webhook_id": "coverage-webhook-id",
        }

    async def list_tokens(self):
        return [WatchedToken(MINT, "Wrapped SOL", "SOL", 9, 1)]

    async def list_monitored_addresses(self):
        return [MINT, ATA_ONE]

    async def get_setting(self, key):
        return self.settings.get(key)

    async def set_setting(self, key, value):
        self.settings[key] = value


class HeliusCoverageTests(unittest.IsolatedAsyncioTestCase):
    def test_schema_accepts_full_spl_decimal_range(self) -> None:
        self.assertIn("decimals BETWEEN 0 AND 255", SCHEMA)

    def test_extracts_token_accounts_from_enhanced_events(self) -> None:
        observed = extract_token_accounts(
            [
                {
                    "accountData": [
                        {
                            "tokenBalanceChanges": [
                                {"mint": MINT, "tokenAccount": ATA_ONE}
                            ]
                        }
                    ],
                    "tokenTransfers": [
                        {
                            "mint": MINT,
                            "fromTokenAccount": ATA_ONE,
                            "toTokenAccount": ATA_TWO,
                        }
                    ],
                }
            ]
        )
        self.assertEqual(observed, {MINT: {ATA_ONE, ATA_TWO}})

    async def test_discovers_token_accounts_for_both_token_programs(self) -> None:
        calls = 0

        def handler(request: httpx.Request) -> httpx.Response:
            nonlocal calls
            calls += 1
            pubkey = ATA_ONE if calls == 1 else ATA_TWO
            return httpx.Response(
                200, json={"jsonrpc": "2.0", "result": [{"pubkey": pubkey}]}
            )

        client = HeliusClient("key", "secret", "https://app.test", "helius")
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            addresses = await client.discover_token_accounts(MINT)
        finally:
            await client.close()
        self.assertEqual(addresses, {ATA_ONE, ATA_TWO})

    async def test_matching_active_webhook_is_not_mutated(self) -> None:
        methods: list[str] = []

        def handler(request: httpx.Request) -> httpx.Response:
            methods.append(request.method)
            coverage = request.url.path.endswith("coverage-webhook-id")
            return httpx.Response(
                200,
                json={
                    "webhookID": "coverage-webhook-id" if coverage else "webhook-id",
                    "webhookURL": "https://app.test/helius",
                    "transactionTypes": ["ANY"] if coverage else ["BUY", "SWAP"],
                    "accountAddresses": [MINT] if coverage else [ATA_ONE, MINT],
                    "authHeader": "secret",
                    "active": True,
                },
            )

        client = HeliusClient("key", "secret", "https://app.test", "helius")
        await client.http.aclose()
        client.http = httpx.AsyncClient(transport=httpx.MockTransport(handler))
        try:
            await client.sync_webhook(SyncDatabase())
        finally:
            await client.close()
        self.assertEqual(methods, ["GET", "GET"])

    async def test_add_reconciles_remote_after_local_rollback(self) -> None:
        token = WatchedToken(MINT, "Wrapped SOL", "SOL", 9, 1)

        class RollbackDatabase:
            def __init__(self):
                self.token = None
                self.accounts = set()

            async def get_token(self, mint):
                return self.token if self.token and self.token.mint == mint else None

            async def list_tokens(self):
                return [self.token] if self.token else []

            async def list_monitored_addresses(self):
                return ([MINT] + sorted(self.accounts)) if self.token else []

            async def add_token(self, value):
                self.token = value
                return True

            async def remove_token(self, mint):
                removed, self.token = self.token, None
                self.accounts = set()
                return removed

            async def replace_token_accounts(self, mint, addresses):
                self.accounts = set(addresses)
                return True

        class PartiallyFailingHelius:
            max_monitored_addresses = 100_000

            def __init__(self):
                self.sync_states = []

            async def validate_mint(self, mint, added_by):
                return token

            async def discover_token_accounts(self, mint):
                return {ATA_ONE}

            async def sync_webhook(self, database):
                self.sync_states.append(await database.list_monitored_addresses())
                if len(self.sync_states) == 1:
                    raise HeliusError("second webhook update failed")

        database = RollbackDatabase()
        helius = PartiallyFailingHelius()
        watchlist = WatchlistService(database, helius)

        with self.assertRaises(HeliusError):
            await watchlist.add(MINT, 1)

        self.assertEqual(helius.sync_states, [[MINT, ATA_ONE], []])
        self.assertIsNone(database.token)


class EventLeaseTests(unittest.IsolatedAsyncioTestCase):
    async def test_startup_does_not_requeue_fresh_processing_events(self) -> None:
        statements = []

        class Connection:
            async def execute(self, statement, *args):
                statements.append(statement)

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield Connection()

            async def close(self):
                return None

        async def create_pool(*args, **kwargs):
            return Pool()

        with patch("bot.database.asyncpg.create_pool", side_effect=create_pool):
            database = Database("postgresql://test")
            await database.connect()

        self.assertEqual(statements, [SCHEMA])

    async def test_claim_reclaims_only_stale_processing_events(self) -> None:
        queries = []

        class Transaction:
            async def __aenter__(self):
                return self

            async def __aexit__(self, *args):
                return None

        class Connection:
            def transaction(self):
                return Transaction()

            async def fetchrow(self, query, *args):
                queries.append(query)
                return None

        class Pool:
            @asynccontextmanager
            async def acquire(self):
                yield Connection()

        database = Database("postgresql://test")
        database.pool = Pool()
        self.assertIsNone(await database.claim_event())
        self.assertIn("status = 'processing'", queries[0])
        self.assertIn("updated_at <=", queries[0])


class ObservingWatchlist:
    def __init__(self) -> None:
        self.observed = []
        self.refreshes = 0

    async def add_observed_accounts(self, accounts):
        self.observed.append(accounts)

    async def refresh_all(self):
        self.refreshes += 1


class TokenAccountRefresherTests(unittest.IsolatedAsyncioTestCase):
    def test_observe_queues_raw_events_without_parsing_them(self) -> None:
        refresher = TokenAccountRefresher(ObservingWatchlist(), 3600)
        events = [{"tokenTransfers": [{"mint": MINT}]}]
        with patch("bot.solana.extract_token_accounts") as extract:
            refresher.observe(events)
        extract.assert_not_called()

    async def test_observed_accounts_wake_refresher_immediately(self) -> None:
        watchlist = ObservingWatchlist()
        refresher = TokenAccountRefresher(watchlist, 3600)
        task = asyncio.create_task(refresher.run())
        refresher.observe(
            [
                {
                    "type": "SWAP",
                    "tokenTransfers": [
                        {
                            "mint": MINT,
                            "fromTokenAccount": ATA_ONE,
                            "toTokenAccount": ATA_TWO,
                        }
                    ],
                }
            ]
        )
        await asyncio.sleep(0.01)
        refresher.stop()
        await task
        self.assertIn({MINT: {ATA_ONE, ATA_TWO}}, watchlist.observed)

    async def test_failed_observations_are_merged_into_next_attempt(self) -> None:
        class FailsOnceWatchlist:
            def __init__(self):
                self.calls = []
                self.first_started = asyncio.Event()
                self.release_first = asyncio.Event()
                self.second_completed = asyncio.Event()

            async def add_observed_accounts(self, accounts):
                self.calls.append(accounts)
                if len(self.calls) == 1:
                    self.first_started.set()
                    await self.release_first.wait()
                    raise RuntimeError("temporary Helius failure")
                self.second_completed.set()

            async def refresh_all(self):
                return None

        watchlist = FailsOnceWatchlist()
        refresher = TokenAccountRefresher(watchlist, 3600)
        refresher.observe(
            [{"tokenTransfers": [{"mint": MINT, "fromTokenAccount": ATA_ONE}]}]
        )
        task = asyncio.create_task(refresher.run())
        await watchlist.first_started.wait()
        refresher.observe(
            [{"tokenTransfers": [{"mint": ATA_ONE, "fromTokenAccount": ATA_TWO}]}]
        )
        watchlist.release_first.set()
        await asyncio.wait_for(watchlist.second_completed.wait(), timeout=1)
        refresher.stop()
        await task
        self.assertEqual(
            watchlist.calls[1],
            {MINT: {ATA_ONE}, ATA_ONE: {ATA_TWO}},
        )


class WorkerDatabase:
    def __init__(self, *, configured: bool, fail_first_claim: bool = False) -> None:
        self.configured = configured
        self.fail_first_claim = fail_first_claim
        self.claims = 0

    async def get_setting(self, key):
        return "-1001" if self.configured else None

    async def claim_event(self):
        self.claims += 1
        if self.fail_first_claim and self.claims == 1:
            raise RuntimeError("temporary database outage")
        return None


class WorkerResilienceTests(unittest.IsolatedAsyncioTestCase):
    async def test_does_not_claim_events_until_alert_chat_is_configured(self) -> None:
        database = WorkerDatabase(configured=False)
        worker = AlertWorker(database, object(), "missing.mp4", 0.01)
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.03)
        worker.stop()
        await task
        self.assertEqual(database.claims, 0)

    async def test_recovers_after_transient_claim_failure(self) -> None:
        database = WorkerDatabase(configured=True, fail_first_claim=True)
        worker = AlertWorker(database, object(), "missing.mp4", 0.01)
        task = asyncio.create_task(worker.run())
        await asyncio.sleep(0.03)
        worker.stop()
        await task
        self.assertGreaterEqual(database.claims, 2)

    async def test_partial_retry_skips_already_delivered_mint(self) -> None:
        token_one = WatchedToken(MINT, "One", "ONE", 9, 1)
        token_two = WatchedToken(ATA_ONE, "Two", "TWO", 9, 1)
        alerts = [
            BuyAlert(token_one, ATA_TWO, 1, None, None, "sig"),
            BuyAlert(token_two, ATA_TWO, 2, None, None, "sig"),
        ]

        class DeliveryDatabase:
            def __init__(self):
                self.states = {}

            async def alert_delivery_states(self, signature):
                return dict(self.states)

            async def reserve_alert_delivery(self, signature, mint, buyer):
                key = (mint, buyer)
                if key in self.states:
                    return False
                self.states[key] = "sending"
                return True

            async def release_alert_delivery(self, signature, mint, buyer):
                self.states.pop((mint, buyer), None)

            async def mark_alert_delivered(self, signature, mint, buyer, message_id):
                self.states[(mint, buyer)] = "delivered"

        database = DeliveryDatabase()
        worker = AlertWorker(database, object(), "missing.mp4", 0.01)
        sent = []

        async def fail_second(alert):
            sent.append(alert.token.mint)
            if alert.token.mint == ATA_ONE:
                raise RuntimeError("temporary Telegram failure")
            return 101

        worker._send = fail_second
        with self.assertRaises(RuntimeError):
            await worker._deliver_alerts("sig", alerts)

        async def succeed(alert):
            sent.append(alert.token.mint)
            return 202

        worker._send = succeed
        await worker._deliver_alerts("sig", alerts)
        self.assertEqual(sent, [MINT, ATA_ONE, ATA_ONE])

    async def test_same_mint_alerts_for_different_buyers_are_both_sent(self) -> None:
        token = WatchedToken(MINT, "One", "ONE", 9, 1)
        alerts = [
            BuyAlert(token, ATA_ONE, 1, None, None, "sig"),
            BuyAlert(token, ATA_TWO, 2, None, None, "sig"),
        ]

        class DeliveryDatabase:
            def __init__(self):
                self.states = {}

            async def alert_delivery_states(self, signature):
                return dict(self.states)

            async def reserve_alert_delivery(self, signature, mint, buyer):
                key = (mint, buyer)
                if key in self.states:
                    return False
                self.states[key] = "sending"
                return True

            async def release_alert_delivery(self, signature, mint, buyer):
                self.states.pop((mint, buyer), None)

            async def mark_alert_delivered(self, signature, mint, buyer, message_id):
                self.states[(mint, buyer)] = "delivered"

        worker = AlertWorker(DeliveryDatabase(), object(), "missing.mp4", 0.01)
        sent = []

        async def send(alert):
            sent.append(alert.buyer)
            return len(sent)

        worker._send = send
        await worker._deliver_alerts("sig", alerts)
        self.assertEqual(sent, [ATA_ONE, ATA_TWO])

    async def test_successful_send_is_reserved_before_delivery_record_fails(
        self,
    ) -> None:
        token = WatchedToken(MINT, "One", "ONE", 9, 1)
        alert = BuyAlert(token, ATA_ONE, 1, None, None, "sig")

        class DeliveryDatabase:
            def __init__(self):
                self.states = {}
                self.fail_mark = True

            async def alert_delivery_states(self, signature):
                return dict(self.states)

            async def reserve_alert_delivery(self, signature, mint, buyer):
                key = (mint, buyer)
                if key in self.states:
                    return False
                self.states[key] = "sending"
                return True

            async def release_alert_delivery(self, signature, mint, buyer):
                self.states.pop((mint, buyer), None)

            async def mark_alert_delivered(self, signature, mint, buyer, message_id):
                if self.fail_mark:
                    self.fail_mark = False
                    raise RuntimeError("temporary database failure")
                self.states[(mint, buyer)] = "delivered"

        database = DeliveryDatabase()
        worker = AlertWorker(database, object(), "missing.mp4", 0.01)
        sent = []

        async def send(_alert):
            sent.append(_alert.buyer)
            return 101

        worker._send = send
        with self.assertRaises(RuntimeError):
            await worker._deliver_alerts("sig", [alert])
        with self.assertRaises(RuntimeError):
            await worker._deliver_alerts("sig", [alert])
        self.assertEqual(sent, [ATA_ONE])

    async def test_cancelled_send_becomes_uncertain_instead_of_delivered(self) -> None:
        token = WatchedToken(MINT, "One", "ONE", 9, 1)
        alert = BuyAlert(token, ATA_ONE, 1, None, None, "sig")

        class DeliveryDatabase:
            def __init__(self):
                self.states = {}

            async def alert_delivery_states(self, signature):
                return dict(self.states)

            async def reserve_alert_delivery(self, signature, mint, buyer):
                key = (mint, buyer)
                if key in self.states:
                    return False
                self.states[key] = "sending"
                return True

            async def mark_alert_uncertain(self, signature, mint, buyer):
                self.states[(mint, buyer)] = "uncertain"

        database = DeliveryDatabase()
        worker = AlertWorker(database, object(), "missing.mp4", 0.01)

        async def cancel(_alert):
            raise asyncio.CancelledError

        worker._send = cancel
        with self.assertRaises(asyncio.CancelledError):
            await worker._deliver_alerts("sig", [alert])
        self.assertEqual(database.states[(MINT, ATA_ONE)], "uncertain")

        async def must_not_send(_alert):
            self.fail("An uncertain alert must not be sent automatically")

        worker._send = must_not_send
        with self.assertRaises(RuntimeError):
            await worker._deliver_alerts("sig", [alert])

    async def test_animation_timeout_does_not_fall_back_to_duplicate_text(self) -> None:
        token = WatchedToken(MINT, "One", "ONE", 9, 1)
        alert = BuyAlert(token, ATA_ONE, 1, None, None, "sig")

        class DeliveryDatabase:
            def __init__(self):
                self.states = {}

            async def get_setting(self, key):
                return "-1001" if key == "alert_chat_id" else "cached-animation"

            async def set_setting(self, key, value):
                return None

            async def alert_delivery_states(self, signature):
                return dict(self.states)

            async def reserve_alert_delivery(self, signature, mint, buyer):
                self.states[(mint, buyer)] = "sending"
                return True

            async def mark_alert_uncertain(self, signature, mint, buyer):
                self.states[(mint, buyer)] = "uncertain"

            async def mark_alert_delivered(self, signature, mint, buyer, message_id):
                self.states[(mint, buyer)] = "delivered"

        class Bot:
            def __init__(self):
                self.text_sends = 0

            async def send_animation(self, **kwargs):
                raise TimedOut("ambiguous timeout")

            async def send_message(self, **kwargs):
                self.text_sends += 1
                return type("Message", (), {"message_id": 1})()

        database = DeliveryDatabase()
        bot = Bot()
        worker = AlertWorker(database, bot, "missing.mp4", 0.01)

        with self.assertRaises(TimedOut):
            await worker._deliver_alerts("sig", [alert])

        self.assertEqual(bot.text_sends, 0)
        self.assertEqual(database.states[(MINT, ATA_ONE)], "uncertain")

    async def test_definite_telegram_rejection_releases_delivery(self) -> None:
        token = WatchedToken(MINT, "One", "ONE", 9, 1)
        alert = BuyAlert(token, ATA_ONE, 1, None, None, "sig")

        class DeliveryDatabase:
            def __init__(self):
                self.states = {}

            async def get_setting(self, key):
                return "-1001" if key == "alert_chat_id" else None

            async def alert_delivery_states(self, signature):
                return dict(self.states)

            async def reserve_alert_delivery(self, signature, mint, buyer):
                self.states[(mint, buyer)] = "sending"
                return True

            async def release_alert_delivery(self, signature, mint, buyer):
                self.states.pop((mint, buyer), None)

            async def mark_alert_uncertain(self, signature, mint, buyer):
                self.states[(mint, buyer)] = "uncertain"

        class Bot:
            def __init__(self):
                self.text_sends = 0

            async def send_message(self, **kwargs):
                self.text_sends += 1
                raise BadRequest("message rejected")

        database = DeliveryDatabase()
        bot = Bot()
        worker = AlertWorker(database, bot, "missing.mp4", 0.01)

        with self.assertRaises(BadRequest):
            await worker._deliver_alerts("sig", [alert])

        self.assertNotIn((MINT, ATA_ONE), database.states)
        self.assertEqual(bot.text_sends, 1)

    async def test_rate_limit_uses_telegram_delay_without_consuming_retry(self) -> None:
        deferred = []
        generic_retries = []

        class RateLimitedDatabase:
            def __init__(self):
                self.worker = None
                self.claimed = False

            async def get_setting(self, key):
                return "-1001"

            async def claim_event(self):
                if self.claimed:
                    return None
                self.claimed = True
                return type(
                    "Event",
                    (),
                    {"signature": "sig", "payload": {}, "attempts": 4},
                )()

            async def list_tokens(self):
                return []

            async def defer_event(self, signature, delay_seconds, error):
                deferred.append((signature, delay_seconds, error))
                self.worker.stop()

            async def retry_event(self, signature, attempts, error):
                generic_retries.append((signature, attempts, error))
                self.worker.stop()

        database = RateLimitedDatabase()
        worker = AlertWorker(database, object(), "missing.mp4", 0.01)
        database.worker = worker

        async def rate_limited(signature, alerts):
            raise RetryAfter(30)

        worker._deliver_alerts = rate_limited
        with patch("bot.alerts.parse_buys", return_value=[object()]):
            await worker.run()

        self.assertEqual(generic_retries, [])
        self.assertEqual(deferred[0][0], "sig")
        self.assertGreaterEqual(deferred[0][1], 30)


if __name__ == "__main__":
    unittest.main()
