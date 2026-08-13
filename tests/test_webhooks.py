import os
import asyncio
import logging
import subprocess
import sys
import unittest
from types import SimpleNamespace

os.environ.setdefault("BOT_TOKEN", "123456:ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghi")
os.environ.setdefault("OWNER_USER_ID", "123456789")
os.environ.setdefault("DATABASE_URL", "postgresql://user:pass@localhost/solducks")
os.environ.setdefault("RENDER_EXTERNAL_URL", "https://example.com")
os.environ.setdefault("WEBHOOK_SECRET", "telegram-test-secret")
os.environ.setdefault("HELIUS_API_KEY", "helius-test-key")
os.environ.setdefault("HELIUS_WEBHOOK_SECRET", "Bearer-helius-test-secret")

from httpx import ASGITransport, AsyncClient
from starlette.applications import Starlette
from starlette.routing import Route

from bot import __main__ as entrypoint
from bot.config import setup_logging
from bot.commands import (
    OWNER_COMMANDS,
    OPERATOR_COMMANDS,
    PUBLIC_COMMANDS,
    _exempt_identifier,
    disallow_command,
    exempt_command,
    exemptlist_command,
    menu_callback,
    retrydead_command,
    retryuncertain_command,
    setalert_command,
    unexempt_command,
    wizard_input,
)


class FakeDatabase:
    def __init__(self) -> None:
        self.events = []

    async def enqueue_events(self, events):
        self.events.extend(events)
        return len(events)


class CommandMenuTests(unittest.TestCase):
    def test_setup_logging_suppresses_sensitive_http_urls(self) -> None:
        setup_logging()
        self.assertGreaterEqual(logging.getLogger("httpx").level, logging.WARNING)
        self.assertGreaterEqual(logging.getLogger("httpcore").level, logging.WARNING)

    def test_only_id_is_public(self) -> None:
        self.assertEqual([command.command for command in PUBLIC_COMMANDS], ["id"])
        self.assertIn("watch", [command.command for command in OPERATOR_COMMANDS])
        self.assertIn("menu", [command.command for command in OPERATOR_COMMANDS])
        self.assertIn("allow", [command.command for command in OWNER_COMMANDS])
        self.assertIn("retrydead", [command.command for command in OWNER_COMMANDS])
        self.assertIn("exempt", [command.command for command in OWNER_COMMANDS])
        self.assertIn("exemptlist", [command.command for command in OWNER_COMMANDS])
        self.assertNotIn("allow", [command.command for command in OPERATOR_COMMANDS])
        self.assertNotIn("exempt", [command.command for command in OPERATOR_COMMANDS])
        self.assertNotIn(
            "retrydead", [command.command for command in OPERATOR_COMMANDS]
        )

    def test_slow_watchlist_commands_do_not_block_update_processing(self) -> None:
        handlers = [
            handler
            for group in entrypoint.application.handlers.values()
            for handler in group
            if getattr(handler, "commands", None)
        ]
        by_command = {
            command: handler for handler in handlers for command in handler.commands
        }
        self.assertFalse(by_command["watch"].block)
        self.assertFalse(by_command["unwatch"].block)

    def test_registers_button_menu_and_private_input_wizard(self) -> None:
        callbacks = [
            handler.callback.__name__
            for group in entrypoint.application.handlers.values()
            for handler in group
            if hasattr(handler, "callback")
        ]
        self.assertIn("menu_callback", callbacks)
        self.assertIn("wizard_input", callbacks)

    def test_rejects_non_positive_runtime_intervals_at_startup(self) -> None:
        environment = os.environ.copy()
        environment["ALERT_WORKER_POLL_SECONDS"] = "0"
        result = subprocess.run(
            [sys.executable, "-c", "import bot.config"],
            cwd=os.getcwd(),
            env=environment,
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("ALERT_WORKER_POLL_SECONDS", result.stderr)


class ButtonWizardTests(unittest.IsolatedAsyncioTestCase):
    async def test_operator_can_watch_with_private_button_wizard(self) -> None:
        mint = "So11111111111111111111111111111111111111112"
        replies = []
        watched = []

        class Database:
            async def is_operator(self, user_id):
                return user_id == 42

        class Registry:
            async def register_user(self, user_id, *, owner=False):
                return True

        class Watchlist:
            async def add(self, value, user_id):
                watched.append((value, user_id))
                return SimpleNamespace(symbol="DUCK", name=None, mint=value), True

        async def answer(**kwargs):
            return None

        async def reply(text, **kwargs):
            replies.append(text)

        message = SimpleNamespace(reply_text=reply, text=None)
        update = SimpleNamespace(
            callback_query=SimpleNamespace(data="solducks:watch", answer=answer),
            effective_message=message,
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(type="private"),
        )
        context = SimpleNamespace(
            args=[],
            user_data={},
            application=SimpleNamespace(
                bot_data={
                    "database": Database(),
                    "watchlist": Watchlist(),
                    "command_registry": Registry(),
                }
            ),
        )

        await menu_callback(update, context)
        self.assertEqual(context.user_data["solducks_wizard"], "watch")

        message.text = mint
        update.callback_query = None
        await wizard_input(update, context)

        self.assertEqual(watched, [(mint, 42)])
        self.assertNotIn("solducks_wizard", context.user_data)
        self.assertTrue(any("Now watching DUCK" in reply for reply in replies))

    async def test_operator_cannot_start_owner_wizard_from_forged_button(self) -> None:
        replies = []

        class Database:
            async def is_operator(self, user_id):
                return True

        class Registry:
            async def register_user(self, user_id, *, owner=False):
                return True

        async def answer(**kwargs):
            return None

        async def reply(text, **kwargs):
            replies.append(text)

        update = SimpleNamespace(
            callback_query=SimpleNamespace(data="solducks:allow", answer=answer),
            effective_message=SimpleNamespace(reply_text=reply),
            effective_user=SimpleNamespace(id=42),
            effective_chat=SimpleNamespace(type="private"),
        )
        context = SimpleNamespace(
            args=[],
            user_data={},
            application=SimpleNamespace(
                bot_data={
                    "database": Database(),
                    "watchlist": object(),
                    "command_registry": Registry(),
                }
            ),
        )

        await menu_callback(update, context)

        self.assertEqual(context.user_data, {})
        self.assertTrue(any("restricted" in reply.lower() for reply in replies))


class ExemptionTests(unittest.IsolatedAsyncioTestCase):
    @staticmethod
    def _update(args, database):
        replies = []

        class Registry:
            async def register_user(self, user_id, *, owner=False):
                return True

        async def reply(text, **kwargs):
            replies.append(text)

        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=reply),
            effective_user=SimpleNamespace(id=123456789),
            effective_chat=SimpleNamespace(type="private"),
        )
        context = SimpleNamespace(
            args=args,
            application=SimpleNamespace(
                bot_data={
                    "database": database,
                    "watchlist": object(),
                    "command_registry": Registry(),
                }
            ),
        )
        return update, context, replies

    class ExemptDatabase:
        def __init__(self):
            self.exempt = []

        async def add_exempt(self, identifier, added_by):
            if identifier in self.exempt:
                return False
            self.exempt.append(identifier)
            return True

        async def remove_exempt(self, identifier):
            if identifier in self.exempt:
                self.exempt.remove(identifier)
                return True
            return False

        async def list_exempt(self):
            return list(self.exempt)

    async def test_exempt_adds_and_lists_bot_id(self) -> None:
        database = self.ExemptDatabase()
        update, context, replies = self._update(["879589599062679552"], database)
        await exempt_command(update, context)
        self.assertTrue(any("exempted" in r for r in replies))

        update, context, replies = self._update([], database)
        await exemptlist_command(update, context)
        self.assertTrue(any("879589599062679552" in r for r in replies))

    async def test_exempt_accepts_username_and_name(self) -> None:
        database = self.ExemptDatabase()
        update, context, replies = self._update(["@RoseMusicBot"], database)
        await exempt_command(update, context)
        self.assertTrue(any("exempted" in r for r in replies))
        self.assertEqual(database.exempt, ["@rosemusicbot"])

        update, context, replies = self._update(["Rose Music Bot"], database)
        await exempt_command(update, context)
        self.assertTrue(any("exempted" in r for r in replies))
        self.assertEqual(database.exempt, ["@rosemusicbot", "name:rose music bot"])

    async def test_unexempt_removes_bot_id(self) -> None:
        database = self.ExemptDatabase()
        update, context, _ = self._update(["879589599062679552"], database)
        await exempt_command(update, context)

        update, context, replies = self._update(["879589599062679552"], database)
        await unexempt_command(update, context)
        self.assertTrue(any("removed" in r for r in replies))

    def test_exempt_identifier_normalization(self) -> None:
        self.assertEqual(_exempt_identifier("879589599062679552"), "879589599062679552")
        self.assertEqual(_exempt_identifier("@RoseMusicBot"), "@rosemusicbot")
        self.assertEqual(_exempt_identifier("Rose Music Bot"), "name:rose music bot")
        self.assertIsNone(_exempt_identifier(""))
        self.assertIsNone(_exempt_identifier("@"))
        self.assertIsNone(_exempt_identifier(str(2**63)))


class AlertDestinationTests(unittest.IsolatedAsyncioTestCase):
    async def test_rejects_channel_when_bot_cannot_post(self) -> None:
        replies = []

        class Database:
            def __init__(self):
                self.settings = {}

            async def set_setting(self, key, value):
                self.settings[key] = value

            async def requeue_dead_events(self):
                return 0

        class Registry:
            async def register_user(self, user_id, *, owner=False):
                return True

        class Bot:
            id = 999

            async def get_chat(self, chat_id):
                return SimpleNamespace(
                    id=chat_id,
                    type="channel",
                    title="Alerts",
                    full_name=None,
                    permissions=None,
                )

            async def get_chat_member(self, chat_id, user_id):
                return SimpleNamespace(status="administrator", can_post_messages=False)

        database = Database()
        message = SimpleNamespace(reply_text=self._reply(replies))
        update = SimpleNamespace(
            effective_message=message,
            effective_user=SimpleNamespace(id=123456789),
            effective_chat=SimpleNamespace(type="private"),
        )
        context = SimpleNamespace(
            args=["-100123"],
            bot=Bot(),
            application=SimpleNamespace(
                bot_data={
                    "database": database,
                    "watchlist": object(),
                    "command_registry": Registry(),
                }
            ),
        )

        await setalert_command(update, context)

        self.assertEqual(database.settings, {})
        self.assertTrue(any("permission" in reply.lower() for reply in replies))

    @staticmethod
    def _reply(replies):
        async def reply(text, **kwargs):
            replies.append(text)

        return reply

    async def test_retrydead_leaves_uncertain_deliveries_for_confirmation(self) -> None:
        calls = []

        class Database:
            async def requeue_dead_events(self, *, clear_uncertain=False):
                calls.append(clear_uncertain)
                return 2

        class Registry:
            async def register_user(self, user_id, *, owner=False):
                return True

        replies = []
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=self._reply(replies)),
            effective_user=SimpleNamespace(id=123456789),
            effective_chat=SimpleNamespace(type="private"),
        )
        context = SimpleNamespace(
            args=[],
            application=SimpleNamespace(
                bot_data={
                    "database": Database(),
                    "watchlist": object(),
                    "command_registry": Registry(),
                }
            ),
        )

        await retrydead_command(update, context)

        self.assertEqual(calls, [False])
        self.assertTrue(any("uncertain" in reply.lower() for reply in replies))

    async def test_retryuncertain_requires_confirmation_before_clearing(self) -> None:
        calls = []

        class Database:
            async def requeue_dead_events(self, *, clear_uncertain=False):
                calls.append(clear_uncertain)
                return 1

        class Registry:
            async def register_user(self, user_id, *, owner=False):
                return True

        replies = []
        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=self._reply(replies)),
            effective_user=SimpleNamespace(id=123456789),
            effective_chat=SimpleNamespace(type="private"),
        )
        context = SimpleNamespace(
            args=[],
            application=SimpleNamespace(
                bot_data={
                    "database": Database(),
                    "watchlist": object(),
                    "command_registry": Registry(),
                }
            ),
        )

        await retryuncertain_command(update, context)
        self.assertEqual(calls, [])

        context.args = ["confirm"]
        await retryuncertain_command(update, context)
        self.assertEqual(calls, [True])

    async def test_disallow_rejects_user_id_outside_bigint_range(self) -> None:
        removals = []
        replies = []

        class Database:
            async def remove_operator(self, user_id):
                removals.append(user_id)
                return False

        class Registry:
            async def register_user(self, user_id, *, owner=False):
                return True

            async def remove_user_scope(self, user_id):
                return None

        update = SimpleNamespace(
            effective_message=SimpleNamespace(reply_text=self._reply(replies)),
            effective_user=SimpleNamespace(id=123456789),
            effective_chat=SimpleNamespace(type="private"),
        )
        context = SimpleNamespace(
            args=[str(2**63)],
            application=SimpleNamespace(
                bot_data={
                    "database": Database(),
                    "watchlist": object(),
                    "command_registry": Registry(),
                }
            ),
        )

        await disallow_command(update, context)

        self.assertEqual(removals, [])
        self.assertTrue(any("usage" in reply.lower() for reply in replies))


class HeliusWebhookTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.database = FakeDatabase()
        entrypoint.application.bot_data["database"] = self.database
        app = Starlette(
            routes=[
                Route("/helius/webhook", entrypoint.helius_webhook, methods=["POST"])
            ]
        )
        self.client = AsyncClient(
            transport=ASGITransport(app=app), base_url="https://test.local"
        )

    async def asyncTearDown(self) -> None:
        entrypoint.application.bot_data.pop("database", None)
        await self.client.aclose()

    async def test_rejects_missing_authentication(self) -> None:
        response = await self.client.post(
            "/helius/webhook", json=[{"signature": "abc"}]
        )
        self.assertEqual(response.status_code, 403)
        self.assertEqual(self.database.events, [])

    async def test_accepts_authenticated_event_batch(self) -> None:
        events = [{"signature": "abc"}, {"signature": "def"}]
        response = await self.client.post(
            "/helius/webhook",
            json=events,
            headers={"Authorization": "Bearer-helius-test-secret"},
        )
        self.assertEqual(response.status_code, 200)
        self.assertEqual(self.database.events, events)

    async def test_rejects_non_json_content(self) -> None:
        response = await self.client.post(
            "/helius/webhook",
            content="not json",
            headers={
                "Authorization": "Bearer-helius-test-secret",
                "Content-Type": "text/plain",
            },
        )
        self.assertEqual(response.status_code, 415)

    async def test_returns_retryable_response_before_persistence_deadline(self) -> None:
        class SlowDatabase:
            async def enqueue_events(self, events):
                await asyncio.sleep(0.05)

        entrypoint.application.bot_data["database"] = SlowDatabase()
        original_timeout = getattr(entrypoint, "HELIUS_ACK_TIMEOUT_SECONDS", None)
        entrypoint.HELIUS_ACK_TIMEOUT_SECONDS = 0.01
        loop = asyncio.get_running_loop()
        started = loop.time()
        try:
            response = await self.client.post(
                "/helius/webhook",
                json=[{"signature": "slow"}],
                headers={"Authorization": "Bearer-helius-test-secret"},
            )
        finally:
            if original_timeout is None:
                del entrypoint.HELIUS_ACK_TIMEOUT_SECONDS
            else:
                entrypoint.HELIUS_ACK_TIMEOUT_SECONDS = original_timeout
        elapsed = loop.time() - started

        self.assertEqual(response.status_code, 503)
        self.assertLess(elapsed, 0.04)


if __name__ == "__main__":
    unittest.main()
