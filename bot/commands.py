"""SolDucks public, operator, and owner Telegram commands."""

from __future__ import annotations

import logging

from telegram import (
    Bot,
    BotCommand,
    BotCommandScopeChat,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.constants import ChatMemberStatus, ChatType
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from bot.config import OWNER_USER_ID
from bot.database import Database
from bot.solana import HeliusError, WatchlistService

logger = logging.getLogger(__name__)

PUBLIC_COMMANDS = [BotCommand("id", "Show your Telegram user ID")]
OPERATOR_COMMANDS = PUBLIC_COMMANDS + [
    BotCommand("menu", "Open the SolDucks control panel"),
    BotCommand("watch", "Watch a Solana token mint"),
    BotCommand("unwatch", "Stop watching a token mint"),
    BotCommand("tokens", "List watched token mints"),
    BotCommand("status", "Show SolDucks alert status"),
]
OWNER_COMMANDS = OPERATOR_COMMANDS + [
    BotCommand("allow", "Add an operator Telegram ID"),
    BotCommand("disallow", "Remove an operator Telegram ID"),
    BotCommand("users", "List allowlisted operators"),
    BotCommand("setalert", "Set the alert Telegram chat ID"),
    BotCommand("retrydead", "Replay dead-lettered alerts"),
    BotCommand("retryuncertain", "Confirm replay of uncertain alerts"),
]

WIZARD_ACTIONS = {
    "watch": ("Send the Solana token mint to watch.", "token mint"),
    "unwatch": ("Send the Solana token mint to remove.", "token mint"),
    "allow": ("Send the Telegram user ID to allow.", "user ID"),
    "disallow": ("Send the Telegram user ID to remove.", "user ID"),
    "setalert": ("Send the destination Telegram chat ID.", "chat ID"),
}
OWNER_MENU_ACTIONS = {"allow", "disallow", "users", "setalert", "retryuncertain"}


class CommandRegistry:
    def __init__(self, bot: Bot, database: Database) -> None:
        self.bot = bot
        self.database = database

    async def register_all(self) -> None:
        await self.bot.set_my_commands(PUBLIC_COMMANDS)
        await self.register_user(OWNER_USER_ID, owner=True)
        for user_id in await self.database.list_operators():
            await self.register_user(user_id)

    async def register_user(self, user_id: int, *, owner: bool = False) -> bool:
        try:
            await self.bot.set_my_commands(
                OWNER_COMMANDS
                if owner or user_id == OWNER_USER_ID
                else OPERATOR_COMMANDS,
                scope=BotCommandScopeChat(chat_id=user_id),
            )
            return True
        except TelegramError as exc:
            # Telegram may reject a private-chat scope until the user messages the bot.
            logger.info("Could not register commands for %s yet: %s", user_id, exc)
            return False

    async def remove_user_scope(self, user_id: int) -> None:
        try:
            await self.bot.delete_my_commands(
                scope=BotCommandScopeChat(chat_id=user_id)
            )
        except TelegramError as exc:
            logger.info("Could not remove command scope for %s: %s", user_id, exc)


def _services(
    context: ContextTypes.DEFAULT_TYPE,
) -> tuple[Database, WatchlistService, CommandRegistry]:
    return (
        context.application.bot_data["database"],
        context.application.bot_data["watchlist"],
        context.application.bot_data["command_registry"],
    )


async def _authorize(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *, owner: bool = False
) -> bool:
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return False
    if chat.type != ChatType.PRIVATE:
        await message.reply_text(
            "Management commands are available only in the bot's DM."
        )
        return False
    database, _, registry = _services(context)
    allowed = user.id == OWNER_USER_ID or (
        not owner and await database.is_operator(user.id)
    )
    if not allowed:
        await message.reply_text("This command is restricted.")
        return False
    await registry.register_user(user.id, owner=user.id == OWNER_USER_ID)
    return True


def _one_arg(context: ContextTypes.DEFAULT_TYPE) -> str | None:
    if len(context.args) != 1:
        return None
    return context.args[0].strip()


async def id_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not update.effective_message or not update.effective_user:
        return
    database, _, registry = _services(context)
    reply_markup = None
    if update.effective_chat and update.effective_chat.type == ChatType.PRIVATE:
        if update.effective_user.id == OWNER_USER_ID:
            await registry.register_user(update.effective_user.id, owner=True)
            reply_markup = _menu_keyboard(owner=True)
        elif await database.is_operator(update.effective_user.id):
            await registry.register_user(update.effective_user.id)
            reply_markup = _menu_keyboard(owner=False)
    await update.effective_message.reply_text(
        f"Your Telegram user ID is: `{update.effective_user.id}`",
        parse_mode="Markdown",
        reply_markup=reply_markup,
    )


async def menu_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorize(update, context):
        return
    owner = update.effective_user.id == OWNER_USER_ID
    context.user_data.pop("solducks_wizard", None)
    await update.effective_message.reply_text(
        "🦆 SolDucks control panel\nChoose an action:",
        reply_markup=_menu_keyboard(owner=owner),
    )


def _menu_keyboard(*, owner: bool) -> InlineKeyboardMarkup:
    rows = [
        [
            InlineKeyboardButton("➕ Watch token", callback_data="solducks:watch"),
            InlineKeyboardButton("➖ Unwatch", callback_data="solducks:unwatch"),
        ],
        [
            InlineKeyboardButton("🪙 Tokens", callback_data="solducks:tokens"),
            InlineKeyboardButton("📊 Status", callback_data="solducks:status"),
        ],
    ]
    if owner:
        rows.extend(
            [
                [
                    InlineKeyboardButton(
                        "✅ Allow user", callback_data="solducks:allow"
                    ),
                    InlineKeyboardButton(
                        "⛔ Remove user", callback_data="solducks:disallow"
                    ),
                ],
                [
                    InlineKeyboardButton("👥 Users", callback_data="solducks:users"),
                    InlineKeyboardButton(
                        "📣 Alert chat", callback_data="solducks:setalert"
                    ),
                ],
                [
                    InlineKeyboardButton(
                        "♻️ Retry safe", callback_data="solducks:retrydead"
                    ),
                    InlineKeyboardButton(
                        "⚠️ Uncertain", callback_data="solducks:retryuncertain"
                    ),
                ],
            ]
        )
    return InlineKeyboardMarkup(rows)


async def watch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorize(update, context):
        return
    mint = _one_arg(context)
    if mint is None:
        await update.effective_message.reply_text("Usage: /watch <token_mint>")
        return
    _, watchlist, _ = _services(context)
    await update.effective_message.reply_text(
        "Validating the mint and updating Helius…"
    )
    try:
        token, created = await watchlist.add(mint, update.effective_user.id)
    except (ValueError, HeliusError) as exc:
        await update.effective_message.reply_text(str(exc))
        return
    label = token.symbol or token.name or token.mint
    text = f"Now watching {label}." if created else f"{label} is already watched."
    await update.effective_message.reply_text(text)


async def unwatch_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorize(update, context):
        return
    mint = _one_arg(context)
    if mint is None:
        await update.effective_message.reply_text("Usage: /unwatch <token_mint>")
        return
    _, watchlist, _ = _services(context)
    try:
        token = await watchlist.remove(mint)
    except HeliusError as exc:
        await update.effective_message.reply_text(str(exc))
        return
    await update.effective_message.reply_text(
        "That mint was not watched."
        if token is None
        else f"Stopped watching {token.symbol or token.name or token.mint}."
    )


async def tokens_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorize(update, context):
        return
    database, _, _ = _services(context)
    tokens = await database.list_tokens()
    if not tokens:
        await update.effective_message.reply_text(
            "No token mints are currently watched."
        )
        return
    lines = [
        f"{index}. {token.symbol or token.name or 'Unknown'} — {token.mint}"
        for index, token in enumerate(tokens, start=1)
    ]
    await _reply_chunks(update, "Watched token mints:\n" + "\n".join(lines))


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorize(update, context):
        return
    database, _, _ = _services(context)
    tokens = await database.list_tokens()
    monitored_addresses = await database.list_monitored_addresses()
    alert_chat = await database.get_setting("alert_chat_id")
    webhook_id = await database.get_setting("helius_webhook_id")
    coverage_webhook_id = await database.get_setting("helius_coverage_webhook_id")
    counts = await database.event_counts()
    uncertain = await database.uncertain_delivery_count()
    await update.effective_message.reply_text(
        "SolDucks status\n"
        f"Watched mints: {len(tokens)}\n"
        f"Monitored mint/token accounts: {len(monitored_addresses)}\n"
        f"Alert chat: {alert_chat or 'not configured'}\n"
        "Helius webhooks: "
        f"{'configured' if webhook_id and coverage_webhook_id else 'not fully configured'}\n"
        f"Delivered events: {counts.get('delivered', 0)}\n"
        f"Pending/retrying: {counts.get('pending', 0) + counts.get('failed', 0)}\n"
        f"Dead-letter events: {counts.get('dead', 0)}\n"
        f"Uncertain Telegram deliveries: {uncertain}"
    )


async def allow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorize(update, context, owner=True):
        return
    raw = _one_arg(context)
    try:
        user_id = int(raw or "")
        if user_id <= 0 or user_id >= 2**63:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text(
            "Usage: /allow <positive_telegram_user_id>"
        )
        return
    database, _, registry = _services(context)
    if user_id == OWNER_USER_ID:
        await update.effective_message.reply_text("The owner already has full access.")
        return
    created = await database.add_operator(user_id, OWNER_USER_ID)
    registered = await registry.register_user(user_id)
    suffix = (
        ""
        if registered
        else " They will see their command menu after first messaging the bot."
    )
    await update.effective_message.reply_text(
        ("Operator added." if created else "That user is already an operator.") + suffix
    )


async def disallow_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorize(update, context, owner=True):
        return
    raw = _one_arg(context)
    try:
        user_id = int(raw or "")
        if user_id <= 0 or user_id >= 2**63:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Usage: /disallow <telegram_user_id>")
        return
    if user_id == OWNER_USER_ID:
        await update.effective_message.reply_text(
            "The configured owner cannot be removed."
        )
        return
    database, _, registry = _services(context)
    removed = await database.remove_operator(user_id)
    await registry.remove_user_scope(user_id)
    await update.effective_message.reply_text(
        "Operator removed." if removed else "That user was not allowlisted."
    )


async def users_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorize(update, context, owner=True):
        return
    database, _, _ = _services(context)
    users = await database.list_operators()
    lines = [f"Owner: {OWNER_USER_ID}"] + [f"Operator: {user_id}" for user_id in users]
    await _reply_chunks(update, "Allowlisted users:\n" + "\n".join(lines))


async def setalert_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorize(update, context, owner=True):
        return
    raw = _one_arg(context)
    try:
        chat_id = int(raw or "")
        if chat_id == 0 or not -(2**63) < chat_id < 2**63:
            raise ValueError
    except ValueError:
        await update.effective_message.reply_text("Usage: /setalert <telegram_chat_id>")
        return
    try:
        chat = await context.bot.get_chat(chat_id)
        if not await _can_post_alerts(context.bot, chat):
            await update.effective_message.reply_text(
                "The bot does not have permission to post messages in that chat."
            )
            return
    except TelegramError:
        await update.effective_message.reply_text(
            "The bot cannot access that chat. Add it there first and check the chat ID."
        )
        return
    database, _, _ = _services(context)
    await database.set_setting("alert_chat_id", str(chat_id))
    requeued = await database.requeue_dead_events()
    await update.effective_message.reply_text(
        f"Buy alerts will be sent to {chat.title or chat.full_name or chat_id}. "
        f"Requeued {requeued} dead-lettered event(s)."
    )


async def _can_post_alerts(bot: Bot, chat: object) -> bool:
    """Check effective send permission without posting a test message."""
    chat_type = getattr(chat, "type", None)
    if chat_type == ChatType.PRIVATE:
        return True

    member = await bot.get_chat_member(getattr(chat, "id"), bot.id)
    status = member.status
    if status == ChatMemberStatus.OWNER:
        return True
    if status in {ChatMemberStatus.LEFT, ChatMemberStatus.BANNED}:
        return False
    if chat_type == ChatType.CHANNEL:
        return status == ChatMemberStatus.ADMINISTRATOR and bool(
            getattr(member, "can_post_messages", False)
        )
    if status == ChatMemberStatus.RESTRICTED:
        return bool(getattr(member, "is_member", False)) and bool(
            getattr(member, "can_send_messages", False)
        )
    if status == ChatMemberStatus.ADMINISTRATOR:
        return True
    permissions = getattr(chat, "permissions", None)
    return (
        permissions is None or getattr(permissions, "can_send_messages", None) is True
    )


async def retrydead_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _authorize(update, context, owner=True):
        return
    database, _, _ = _services(context)
    requeued = await database.requeue_dead_events()
    await update.effective_message.reply_text(
        f"Requeued {requeued} safely retryable dead-lettered event(s). "
        "Uncertain sends were left untouched; use /retryuncertain confirm to replay them."
    )


async def retryuncertain_command(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    if not await _authorize(update, context, owner=True):
        return
    if _one_arg(context) != "confirm":
        await update.effective_message.reply_text(
            "Replaying an uncertain send can create a duplicate. "
            "Use /retryuncertain confirm or the confirmation button to continue."
        )
        return
    database, _, _ = _services(context)
    requeued = await database.requeue_dead_events(clear_uncertain=True)
    await update.effective_message.reply_text(
        f"Cleared uncertain reservations and requeued {requeued} dead-lettered event(s). "
        "A duplicate is possible if Telegram completed an earlier ambiguous send."
    )


async def menu_callback(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if query is None or not query.data:
        return
    await query.answer()
    action = query.data.removeprefix("solducks:")
    owner_action = action in OWNER_MENU_ACTIONS or action == "retryuncertain_confirm"
    if not await _authorize(update, context, owner=owner_action):
        return
    if action == "cancel":
        context.user_data.pop("solducks_wizard", None)
        await update.effective_message.reply_text("Action cancelled.")
        return

    if action in WIZARD_ACTIONS:
        context.user_data["solducks_wizard"] = action
        prompt, _ = WIZARD_ACTIONS[action]
        await update.effective_message.reply_text(
            prompt,
            reply_markup=InlineKeyboardMarkup(
                [[InlineKeyboardButton("Cancel", callback_data="solducks:cancel")]]
            ),
        )
        return
    if action == "retryuncertain":
        await update.effective_message.reply_text(
            "Telegram may already have completed these sends. Replay anyway?",
            reply_markup=InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(
                            "Replay — duplicates possible",
                            callback_data="solducks:retryuncertain_confirm",
                        )
                    ],
                    [InlineKeyboardButton("Cancel", callback_data="solducks:cancel")],
                ]
            ),
        )
        return

    handlers = {
        "menu": menu_command,
        "tokens": tokens_command,
        "status": status_command,
        "users": users_command,
        "retrydead": retrydead_command,
    }
    if action == "retryuncertain_confirm":
        context.args = ["confirm"]
        await retryuncertain_command(update, context)
    elif handler := handlers.get(action):
        await handler(update, context)


async def wizard_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    action = context.user_data.pop("solducks_wizard", None)
    if action not in WIZARD_ACTIONS or not update.effective_message:
        return
    value = (update.effective_message.text or "").strip()
    if not value:
        _, label = WIZARD_ACTIONS[action]
        await update.effective_message.reply_text(f"Please send a valid {label}.")
        context.user_data["solducks_wizard"] = action
        return
    context.args = [value]
    handlers = {
        "watch": watch_command,
        "unwatch": unwatch_command,
        "allow": allow_command,
        "disallow": disallow_command,
        "setalert": setalert_command,
    }
    await handlers[action](update, context)


async def _reply_chunks(update: Update, text: str) -> None:
    message = update.effective_message
    while text:
        if len(text) <= 4000:
            chunk, text = text, ""
        else:
            split = text.rfind("\n", 0, 4000)
            if split < 1:
                split = 4000
            chunk, text = text[:split], text[split:].lstrip("\n")
        await message.reply_text(chunk)


def register_command_handlers(application: Application) -> None:
    application.add_handler(CommandHandler("id", id_command))
    application.add_handler(CommandHandler("menu", menu_command))
    # These handlers perform several Solana/Helius requests. Running them as
    # application-managed tasks keeps CAPTCHA and other Telegram updates moving.
    application.add_handler(CommandHandler("watch", watch_command, block=False))
    application.add_handler(CommandHandler("unwatch", unwatch_command, block=False))
    application.add_handler(CommandHandler("tokens", tokens_command))
    application.add_handler(CommandHandler("status", status_command))
    application.add_handler(CommandHandler("allow", allow_command))
    application.add_handler(CommandHandler("disallow", disallow_command))
    application.add_handler(CommandHandler("users", users_command))
    application.add_handler(CommandHandler("setalert", setalert_command))
    application.add_handler(CommandHandler("retrydead", retrydead_command))
    application.add_handler(CommandHandler("retryuncertain", retryuncertain_command))
    application.add_handler(CallbackQueryHandler(menu_callback, pattern=r"^solducks:"))
    application.add_handler(
        MessageHandler(
            filters.ChatType.PRIVATE & filters.TEXT & ~filters.COMMAND,
            wizard_input,
            block=False,
        )
    )
