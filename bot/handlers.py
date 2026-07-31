"""
Telegram event handlers.

- new_member_handler  (F1, F2, F5.1)
- button_handler      (F3, F4)
- timeout_kick        (F5)
- delete_message_job  (cleanup helper)
"""

import logging

from telegram import ChatPermissions, Update
from telegram.error import BadRequest, Forbidden
from telegram.ext import ContextTypes

from bot.captcha import build_keyboard, generate_captcha
from bot.config import AUTO_DELETE_DELAY, DELETE_DELAY, KICK_TIMEOUT, WELCOME_GIF_PATH
from bot.state import get_gif_file_id, pending_users, set_gif_file_id

logger = logging.getLogger(__name__)

# ─── Full permissions granted after successful verification ────────────────────

FULL_PERMISSIONS = ChatPermissions(
    can_send_messages=True,
    can_send_audios=True,
    can_send_documents=True,
    can_send_photos=True,
    can_send_videos=True,
    can_send_video_notes=True,
    can_send_voice_notes=True,
    can_send_polls=True,
    can_send_other_messages=True,
    can_add_web_page_previews=True,
    can_change_info=True,
    can_invite_users=True,
    can_pin_messages=True,
    can_manage_topics=True,
)


# ─── F1 & F2: New member join → mute + CAPTCHA ───────────────────────────────


async def new_member_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Listen for new chat members, mute them, and send a CAPTCHA."""
    if not update.message or not update.message.new_chat_members:
        return

    chat_id = update.message.chat.id
    bot_id = context.bot.id

    for member in update.message.new_chat_members:
        user_id = member.id

        # F1.2: Ignore the bot itself
        if user_id == bot_id:
            continue

        first_name = member.first_name or "User"

        # F1.3: Immediately mute
        try:
            await context.bot.restrict_chat_member(
                chat_id,
                user_id,
                permissions=ChatPermissions(can_send_messages=False),
            )
        except (BadRequest, Forbidden) as exc:
            logger.warning("Failed to mute %s in %s: %s", user_id, chat_id, exc)
            continue

        # F2.1–F2.2: Generate CAPTCHA
        question, correct, choices = generate_captcha()
        keyboard = build_keyboard(user_id, correct, choices)

        caption = (
            f"👋 Welcome, {first_name}!\n\n"
            f"To verify you're human, solve this:\n\n"
            f"**{question}**"
        )

        # F2.3 & F2.4: Send animation with cached file_id when available
        try:
            cached_id = get_gif_file_id()

            if cached_id:
                msg = await context.bot.send_animation(
                    chat_id,
                    animation=cached_id,
                    caption=caption,
                    parse_mode="Markdown",
                    reply_markup=keyboard,
                )
            else:
                with open(WELCOME_GIF_PATH, "rb") as gif_file:
                    msg = await context.bot.send_animation(
                        chat_id,
                        animation=gif_file,
                        caption=caption,
                        parse_mode="Markdown",
                        reply_markup=keyboard,
                    )
                # Cache the file_id after the first successful send
                if msg.animation:
                    set_gif_file_id(msg.animation.file_id)
                    logger.info("Cached GIF file_id: %s", msg.animation.file_id)
        except Exception as exc:
            logger.error("Failed to send welcome GIF to %s: %s", chat_id, exc)
            # Keep user muted — 5-min kick will clean up if CAPTCHA wasn't sent
            continue

        # Pin the CAPTCHA so it doesn't get lost in busy groups
        try:
            await context.bot.pin_chat_message(chat_id, msg.message_id)
        except (BadRequest, Forbidden) as exc:
            logger.warning("Failed to pin CAPTCHA message: %s", exc)

        # Store pending state
        pending_users.set(
            chat_id, user_id,
            correct_answer=correct,
            message_id=msg.message_id,
            first_name=first_name,
        )

        # F5.1: Schedule 5-minute kick timeout
        context.job_queue.run_once(
            timeout_kick,
            when=KICK_TIMEOUT,
            data={"chat_id": chat_id, "user_id": user_id},
            name=f"kick:{chat_id}:{user_id}",
        )

        # Schedule auto-delete of CAPTCHA if user doesn't interact
        context.job_queue.run_once(
            auto_delete_captcha,
            when=AUTO_DELETE_DELAY,
            data={"chat_id": chat_id, "user_id": user_id},
            name=f"autodelete:{chat_id}:{user_id}",
        )

        logger.info(
            "CAPTCHA sent to %s (%s) in chat %s",
            first_name, user_id, chat_id,
        )


# ─── F3 & F4: Inline button presses ──────────────────────────────────────────


async def button_handler(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> None:
    """Handle CAPTCHA answer button clicks."""
    query = update.callback_query
    if not query or not query.data:
        return

    # Parse callback_data: "verify:{user_id}:{chosen}:{correct}"
    parts = query.data.split(":")
    if len(parts) != 4 or parts[0] != "verify":
        return

    target_user_id = int(parts[1])
    chosen = int(parts[2])
    correct = int(parts[3])
    clicker_id = query.from_user.id

    # F3.2: Non-target user clicked
    if clicker_id != target_user_id:
        await query.answer(
            "This verification button is not for you!", show_alert=True
        )
        return

    chat_id = query.message.chat.id

    # Already verified (double-click edge case)
    if not pending_users.has(chat_id, target_user_id):
        await query.answer("Verification already completed.", show_alert=True)
        return

    # F3.3: Incorrect answer
    if chosen != correct:
        await query.answer("Incorrect answer! Try again.", show_alert=False)
        # Cancel auto-delete since user is actively trying
        for job in context.job_queue.get_jobs_by_name(
            f"autodelete:{chat_id}:{target_user_id}"
        ):
            job.schedule_removal()
        return

    # ── F4: Correct answer — verification success ──────────────────────────

    await query.answer("✅ Correct!")

    user_data = pending_users.pop(chat_id, target_user_id)
    first_name = user_data.first_name
    message_id = user_data.message_id

    # F4.1: Cancel the kick job
    job_name = f"kick:{chat_id}:{target_user_id}"
    for job in context.job_queue.get_jobs_by_name(job_name):
        job.schedule_removal()

    # Cancel the auto-delete job
    for job in context.job_queue.get_jobs_by_name(
        f"autodelete:{chat_id}:{target_user_id}"
    ):
        job.schedule_removal()

    # Unpin the CAPTCHA message
    try:
        await context.bot.unpin_chat_message(chat_id, message_id)
    except (BadRequest, Forbidden):
        pass

    # F4.2: Restore full messaging permissions
    try:
        await context.bot.restrict_chat_member(
            chat_id, target_user_id, permissions=FULL_PERMISSIONS
        )
    except (BadRequest, Forbidden) as exc:
        logger.warning("Failed to unmute %s in %s: %s", target_user_id, chat_id, exc)

    # F4.3: Update caption
    try:
        await context.bot.edit_message_caption(
            chat_id,
            message_id,
            caption=f"✅ {first_name} solved the math puzzle and is now verified!",
        )
    except BadRequest as exc:
        logger.warning("Failed to edit caption: %s", exc)

    # F4.4: Schedule message deletion in 10 seconds
    context.job_queue.run_once(
        delete_message_job,
        when=DELETE_DELAY,
        data={"chat_id": chat_id, "message_id": message_id},
        name=f"delmsg:{chat_id}:{message_id}",
    )

    logger.info(
        "User %s (%s) verified in chat %s",
        first_name, target_user_id, chat_id,
    )


# ─── F5: Timeout kick ─────────────────────────────────────────────────────────


async def timeout_kick(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Kick user after 5 minutes of no verification."""
    chat_id = context.job.data["chat_id"]
    user_id = context.job.data["user_id"]

    # Already verified — nothing to do
    user_data = pending_users.pop(chat_id, user_id)
    if user_data is None:
        return

    message_id = user_data.message_id
    first_name = user_data.first_name

    # Unpin the CAPTCHA message
    try:
        await context.bot.unpin_chat_message(chat_id, message_id)
    except (BadRequest, Forbidden):
        pass

    # F5.2: Ban then immediately unban (kick while allowing rejoin)
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await context.bot.unban_chat_member(chat_id, user_id)
    except (BadRequest, Forbidden) as exc:
        logger.warning("Failed to kick %s from %s: %s", user_id, chat_id, exc)

    # Delete the CAPTCHA message
    try:
        await context.bot.delete_message(chat_id, message_id)
    except BadRequest as exc:
        logger.warning("Failed to delete CAPTCHA message: %s", exc)

    logger.info(
        "Kicked %s (%s) from chat %s — verification timed out",
        first_name, user_id, chat_id,
    )


# ─── Auto-delete if user doesn't interact ─────────────────────────────────────


async def auto_delete_captcha(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete the CAPTCHA message if the user hasn't interacted within AUTO_DELETE_DELAY."""
    chat_id = context.job.data["chat_id"]
    user_id = context.job.data["user_id"]

    user_data = pending_users.pop(chat_id, user_id)
    if user_data is None:
        return  # already verified or kicked

    message_id = user_data.message_id
    first_name = user_data.first_name

    # Cancel the kick job since we're cleaning up
    for job in context.job_queue.get_jobs_by_name(f"kick:{chat_id}:{user_id}"):
        job.schedule_removal()

    # Unpin the CAPTCHA message
    try:
        await context.bot.unpin_chat_message(chat_id, message_id)
    except (BadRequest, Forbidden):
        pass

    # Kick the user (ban + unban)
    try:
        await context.bot.ban_chat_member(chat_id, user_id)
        await context.bot.unban_chat_member(chat_id, user_id)
    except (BadRequest, Forbidden) as exc:
        logger.warning("Failed to kick %s from %s: %s", user_id, chat_id, exc)

    # Delete the CAPTCHA message
    try:
        await context.bot.delete_message(chat_id, message_id)
    except BadRequest as exc:
        logger.warning("Failed to delete CAPTCHA message: %s", exc)

    logger.info(
        "Auto-deleted CAPTCHA and kicked %s (%s) from chat %s — no interaction",
        first_name, user_id, chat_id,
    )


# ─── Cleanup helper ───────────────────────────────────────────────────────────


async def delete_message_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Delete a single message (post-verification cleanup)."""
    chat_id = context.job.data["chat_id"]
    message_id = context.job.data["message_id"]
    try:
        await context.bot.delete_message(chat_id, message_id)
    except BadRequest as exc:
        logger.warning("Failed to delete message %s: %s", message_id, exc)
