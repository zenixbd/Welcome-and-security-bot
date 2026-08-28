#!/usr/bin/env python3
"""
================================================================================
 Telegram Verification & Anti-Link Bot (python-telegram-bot v20+ Asynchronous)
================================================================================
Features:
  1. MODULE 1: Auto-Welcome & 3-Channel Force-Subscription Verification
     - Welcomes new members with First Name and User ID.
     - Displays 3 channel invite links with an inline "Verify Membership" button.
     - Concurrently checks membership across all 3 channels via asyncio.gather.
     - Silent fail if not all channels are joined (no action/alert).
     - Success notification and unlock when verified.

  2. MODULE 2: Strike-Based Anti-Link Auto-Moderation
     - Regex-based link scanner for http://, https://, t.me/, telegram.me, and web URLs.
     - Ignores admins and bots.
     - STRIKE 1: Instantly deletes message + sends public warning:
                 "@username, please do not share links. This is your 1st warning."
     - STRIKE 2: Instantly deletes message + permanently bans user via ban_chat_member.
     - Automatic strike reset upon member leaving / rejoining.

  3. ADMIN COMMANDS:
     - /start         : Bot overview & operational status.
     - /stats         : Group member count and active user strike database.
     - /reset_strikes : Admin tool to clear strikes for a specific user (@username or ID).

Requirements:
  - python-telegram-bot >= 20.7
  - python-dotenv >= 1.0.0
================================================================================
"""

import os
import re
import logging
import asyncio
from typing import Optional, Set, Dict, Tuple
from dotenv import load_dotenv

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    ChatMember,
    ChatMemberUpdated,
)
from telegram.constants import ChatMemberStatus, ParseMode
from telegram.error import TelegramError, RetryAfter, BadRequest, Forbidden
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    CallbackQueryHandler,
    ChatMemberHandler,
    ContextTypes,
    filters,
)

# ------------------------------------------------------------------------------
# 1. Environment & Logging Setup
# ------------------------------------------------------------------------------
load_dotenv()

BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
CHANNEL_1 = os.getenv("CHANNEL_1", "").strip()  # e.g., "@channel1" or "-1001234567890"
CHANNEL_2 = os.getenv("CHANNEL_2", "").strip()  # e.g., "@channel2" or "-1001234567891"
CHANNEL_3 = os.getenv("CHANNEL_3", "").strip()  # e.g., "@channel3" or "-1001234567892"
GROUP_ID = os.getenv("GROUP_ID", "").strip()    # Optional restrict to specific group ID

CHANNEL_1_URL = os.getenv("CHANNEL_1_URL", f"https://t.me/{CHANNEL_1.lstrip('@')}")
CHANNEL_2_URL = os.getenv("CHANNEL_2_URL", f"https://t.me/{CHANNEL_2.lstrip('@')}")
CHANNEL_3_URL = os.getenv("CHANNEL_3_URL", f"https://t.me/{CHANNEL_3.lstrip('@')}")

logging.basicConfig(
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    level=logging.INFO
)
logger = logging.getLogger("TelegramVerificationBot")

# In-memory Strike Storage: (chat_id, user_id) -> strike_count
user_strikes: Dict[Tuple[int, int], int] = {}

# In-memory Username cache: username.lower() -> user_id
username_to_id_cache: Dict[str, int] = {}
user_id_to_name_cache: Dict[int, str] = {}

# Set of verified user IDs per chat: (chat_id, user_id)
verified_users: Set[Tuple[int, int]] = set()

# Regex for detecting URLs, Telegram links, and general web domains
LINK_PATTERN = re.compile(
    r"(?i)\b(?:https?://|www\.|t\.me/|telegram\.me/|telegram\.dog/|"
    r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.(?:com|org|net|io|me|xyz|info|app|co|biz|site|live|link|online|cc|ru|in|top|vip|pro|dev|store|club)\b)",
    re.IGNORECASE
)


# ------------------------------------------------------------------------------
# 2. Helper Functions
# ------------------------------------------------------------------------------
def get_user_mention(user) -> str:
    """Returns @username if present, otherwise returns an HTML user mention link."""
    if user.username:
        return f"@{user.username}"
    return f'<a href="tg://user?id={user.id}">{user.first_name}</a>'


async def is_user_admin(chat_id: int, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Checks whether a user is a chat administrator or creator."""
    try:
        member = await context.bot.get_chat_member(chat_id=chat_id, user_id=user_id)
        return member.status in (ChatMemberStatus.ADMINISTRATOR, ChatMemberStatus.OWNER)
    except (BadRequest, Forbidden, TelegramError) as err:
        logger.warning(f"Failed to check admin status for user {user_id} in {chat_id}: {err}")
        return False


async def check_channel_subscription(channel_identifier: str, user_id: int, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """
    Checks if a user is currently a member, administrator, or owner in a target channel.
    """
    if not channel_identifier:
        return True

    try:
        target_chat = int(channel_identifier) if channel_identifier.lstrip("-").isdigit() else channel_identifier
        member = await context.bot.get_chat_member(chat_id=target_chat, user_id=user_id)
        
        valid_statuses = (
            ChatMemberStatus.MEMBER,
            ChatMemberStatus.ADMINISTRATOR,
            ChatMemberStatus.OWNER,
            ChatMemberStatus.RESTRICTED,
        )
        return member.status in valid_statuses
    except (BadRequest, Forbidden, TelegramError) as err:
        logger.warning(f"Error checking channel {channel_identifier} for user {user_id}: {err}")
        return False


# ------------------------------------------------------------------------------
# 3. MODULE 1: Welcome & Verification Handlers
# ------------------------------------------------------------------------------
def build_verification_keyboard() -> InlineKeyboardMarkup:
    """Constructs the inline keyboard with 3 channel links and 1 verify button."""
    keyboard = [
        [InlineKeyboardButton("📢 Channel 1", url=CHANNEL_1_URL)],
        [InlineKeyboardButton("📢 Channel 2", url=CHANNEL_2_URL)],
        [InlineKeyboardButton("📢 Channel 3", url=CHANNEL_3_URL)],
        [InlineKeyboardButton("✅ Verify Membership", callback_data="verify_membership")],
    ]
    return InlineKeyboardMarkup(keyboard)


async def send_welcome_verification(update: Update, context: ContextTypes.DEFAULT_TYPE, new_member):
    """Sends the rich welcome message with channel links and verification button."""
    chat_id = update.effective_chat.id
    user_id = new_member.id
    first_name = new_member.first_name

    # Cache user mapping
    if new_member.username:
        username_to_id_cache[new_member.username.lower().lstrip("@")] = user_id
    user_id_to_name_cache[user_id] = first_name

    welcome_text = (
        f"👋 <b>Welcome to the Group, {first_name}!</b>\n\n"
        f"🆔 <b>User ID:</b> <code>{user_id}</code>\n\n"
        f"⚠️ <b>Verification Required:</b>\n"
        f"To participate in this group, you must join our 3 official channels below and press <b>Verify Membership</b>."
    )

    try:
        await context.bot.send_message(
            chat_id=chat_id,
            text=welcome_text,
            parse_mode=ParseMode.HTML,
            reply_markup=build_verification_keyboard()
        )
        logger.info(f"Sent welcome verification to {first_name} ({user_id}) in chat {chat_id}")
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        await context.bot.send_message(
            chat_id=chat_id,
            text=welcome_text,
            parse_mode=ParseMode.HTML,
            reply_markup=build_verification_keyboard()
        )
    except TelegramError as e:
        logger.error(f"Error sending welcome message: {e}")


async def on_verify_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Handles the 'Verify' button callback.
    Concurrently verifies membership in CHANNEL_1, CHANNEL_2, and CHANNEL_3 using asyncio.gather.
    - If user has joined all 3: sends success message and marks verified.
    - If user has NOT joined all 3: SILENT FAIL (does nothing, answers callback query without message).
    """
    query = update.callback_query
    if not query or not query.data:
        return

    if query.data != "verify_membership":
        return

    user = query.from_user
    user_id = user.id
    chat_id = update.effective_chat.id

    # Check channels concurrently with asyncio.gather
    channels_to_check = [c for c in [CHANNEL_1, CHANNEL_2, CHANNEL_3] if c]
    
    if not channels_to_check:
        all_joined = True
    else:
        results = await asyncio.gather(
            *[check_channel_subscription(ch, user_id, context) for ch in channels_to_check],
            return_exceptions=True
        )
        all_joined = all(res is True for res in results)

    if not all_joined:
        # SILENT FAIL requirement: button does nothing, just acknowledge callback so spinner stops
        try:
            await query.answer()
        except TelegramError:
            pass
        logger.info(f"Verification silent fail for user {user.first_name} ({user_id})")
        return

    # User joined all 3 channels -> Mark as verified
    verified_users.add((chat_id, user_id))
    try:
        await query.answer(text="✅ Verification successful!", show_alert=False)
        success_text = (
            f"🎉 <b>Verification Successful!</b>\n\n"
            f"Welcome {get_user_mention(user)}, you have joined all 3 channels and are now fully verified to chat in this group."
        )
        try:
            await query.edit_message_text(
                text=success_text,
                parse_mode=ParseMode.HTML,
                reply_markup=None  # Remove verification buttons
            )
        except TelegramError:
            await context.bot.send_message(
                chat_id=chat_id,
                text=success_text,
                parse_mode=ParseMode.HTML
            )
        logger.info(f"User {user.first_name} ({user_id}) successfully verified in chat {chat_id}")
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
    except TelegramError as e:
        logger.error(f"Error handling verification success: {e}")


# ------------------------------------------------------------------------------
# 4. MODULE 2: Anti-Link System (Strike-based enforcement)
# ------------------------------------------------------------------------------
async def anti_link_filter_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Monitors all incoming group messages for links.
    - Admin / Bot bypass.
    - Strike 1: Delete message + warning "@username, please do not share links. This is your 1st warning."
    - Strike 2: Delete message + ban user from group.
    """
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat

    if not message or not user or not chat:
        return

    if chat.type not in ("group", "supergroup"):
        return

    if user.is_bot:
        return

    if user.username:
        username_to_id_cache[user.username.lower().lstrip("@")] = user.id
    user_id_to_name_cache[user.id] = user.first_name

    text = message.text or message.caption or ""
    if not text:
        return

    has_link = bool(LINK_PATTERN.search(text))
    if not has_link and message.entities:
        for entity in message.entities:
            if entity.type in ("url", "text_link"):
                has_link = True
                break

    if not has_link:
        return

    # Admin bypass check
    is_admin = await is_user_admin(chat.id, user.id, context)
    if is_admin:
        logger.info(f"Link permitted for admin {user.first_name} ({user.id}) in {chat.id}")
        return

    # Delete the offending message immediately
    try:
        await message.delete()
        logger.info(f"Deleted link message from {user.first_name} ({user.id}) in {chat.id}")
    except (BadRequest, Forbidden) as err:
        logger.warning(f"Could not delete message: {err}")
    except RetryAfter as e:
        await asyncio.sleep(e.retry_after)
        try:
            await message.delete()
        except TelegramError:
            pass

    key = (chat.id, user.id)
    current_strikes = user_strikes.get(key, 0) + 1
    user_strikes[key] = current_strikes

    mention = get_user_mention(user)

    if current_strikes == 1:
        # STRIKE 1: Send public warning
        warning_msg = f"{mention}, please do not share links. This is your 1st warning."
        try:
            await context.bot.send_message(
                chat_id=chat.id,
                text=warning_msg,
                parse_mode=ParseMode.HTML
            )
            logger.info(f"Issued Strike 1 warning to {user.first_name} ({user.id})")
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            await context.bot.send_message(chat_id=chat.id, text=warning_msg, parse_mode=ParseMode.HTML)
        except TelegramError as e:
            logger.error(f"Failed to send warning message: {e}")

    else:
        # STRIKE 2+: Ban user immediately
        try:
            await context.bot.ban_chat_member(chat_id=chat.id, user_id=user.id)
            ban_notice = (
                f"🚫 <b>User Banned</b>\n\n"
                f"{mention} has been banned from the group for posting links after receiving a 1st warning."
            )
            await context.bot.send_message(
                chat_id=chat.id,
                text=ban_notice,
                parse_mode=ParseMode.HTML
            )
            logger.info(f"Banned user {user.first_name} ({user.id}) on Strike {current_strikes} in chat {chat.id}")
        except (BadRequest, Forbidden) as err:
            logger.error(f"Failed to ban user {user.id}: {err}")
            await context.bot.send_message(
                chat_id=chat.id,
                text=f"⚠️ {mention} reached strike {current_strikes}, but bot lacks 'Ban Users' permission to enforce ban.",
                parse_mode=ParseMode.HTML
            )
        except RetryAfter as e:
            await asyncio.sleep(e.retry_after)
            try:
                await context.bot.ban_chat_member(chat_id=chat.id, user_id=user.id)
            except TelegramError:
                pass


# ------------------------------------------------------------------------------
# 5. Member Join/Leave Tracker (Reset strikes on leave/rejoin)
# ------------------------------------------------------------------------------
async def chat_member_updated_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Tracks member status transitions and resets strikes upon leave/rejoin."""
    result: Optional[ChatMemberUpdated] = update.chat_member
    if not result:
        return

    chat_id = result.chat.id
    user = result.new_chat_member.user
    old_status = result.old_chat_member.status
    new_status = result.new_chat_member.status

    if new_status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED):
        user_strikes.pop((chat_id, user.id), None)
        verified_users.discard((chat_id, user.id))
        logger.info(f"Reset strikes for departing user {user.first_name} ({user.id})")
        return

    if old_status in (ChatMemberStatus.LEFT, ChatMemberStatus.BANNED) and new_status == ChatMemberStatus.MEMBER:
        user_strikes.pop((chat_id, user.id), None)
        if not user.is_bot:
            await send_welcome_verification(update, context, user)


async def message_new_chat_members_handler(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message or not message.new_chat_members:
        return

    for new_member in message.new_chat_members:
        if new_member.is_bot:
            continue
        user_strikes.pop((message.chat_id, new_member.id), None)
        await send_welcome_verification(update, context, new_member)


# ------------------------------------------------------------------------------
# 6. Admin Commands: /start, /stats, /reset_strikes
# ------------------------------------------------------------------------------
async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    info_text = (
        f"🤖 <b>Telegram Verification & Anti-Link Bot (v20+ Async)</b>\n\n"
        f"🛡️ <b>Active Modules:</b>\n"
        f"• <b>Module 1:</b> 3-Channel Force-Subscription Verification\n"
        f"• <b>Module 2:</b> 2-Strike Anti-Link Auto-Moderation & Instant Ban\n\n"
        f"⚙️ <b>Configured Channels:</b>\n"
        f"• Channel 1: <code>{CHANNEL_1 or 'Not Set'}</code>\n"
        f"• Channel 2: <code>{CHANNEL_2 or 'Not Set'}</code>\n"
        f"• Channel 3: <code>{CHANNEL_3 or 'Not Set'}</code>\n\n"
        f"👑 <b>Admin Commands:</b>\n"
        f"• <code>/stats</code> - View total members & active strike roster\n"
        f"• <code>/reset_strikes @username</code> - Reset strikes for a user\n\n"
        f"<i>Status: All systems operational.</i>"
    )
    await update.message.reply_text(info_text, parse_mode=ParseMode.HTML)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type in ("group", "supergroup"):
        is_admin = await is_user_admin(chat.id, user.id, context)
        if not is_admin:
            await update.message.reply_text("⛔ This command is restricted to chat administrators.")
            return

    try:
        member_count = await context.bot.get_chat_member_count(chat.id)
    except TelegramError:
        member_count = "N/A"

    chat_strikes = {uid: s for (cid, uid), s in user_strikes.items() if cid == chat.id}

    strikes_lines = []
    for uid, strikes in chat_strikes.items():
        name = user_id_to_name_cache.get(uid, f"User {uid}")
        strikes_lines.append(f"• <b>{name}</b> (<code>{uid}</code>): <b>{strikes}</b> strike(s)")

    strikes_display = "\n".join(strikes_lines) if strikes_lines else "<i>No active strikes in this group.</i>"

    stats_text = (
        f"📊 <b>Group Statistics & Moderation Roster</b>\n\n"
        f"👥 <b>Total Chat Members:</b> {member_count}\n"
        f"⚡ <b>Active Strike Records:</b> {len(chat_strikes)}\n\n"
        f"📋 <b>Current Strike List:</b>\n{strikes_display}"
    )
    await update.message.reply_text(stats_text, parse_mode=ParseMode.HTML)


async def reset_strikes_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    user = update.effective_user

    if chat.type in ("group", "supergroup"):
        is_admin = await is_user_admin(chat.id, user.id, context)
        if not is_admin:
            await update.message.reply_text("⛔ This command is restricted to chat administrators.")
            return

    if not context.args:
        await update.message.reply_text(
            "ℹ️ <b>Usage:</b> <code>/reset_strikes @username</code> or <code>/reset_strikes &lt;user_id&gt;</code>",
            parse_mode=ParseMode.HTML
        )
        return

    target_query = context.args[0].strip()
    target_user_id: Optional[int] = None

    if target_query.lstrip("-").isdigit():
        target_user_id = int(target_query)
    elif target_query.startswith("@"):
        clean_username = target_query.lstrip("@").lower()
        target_user_id = username_to_id_cache.get(clean_username)
    else:
        clean_username = target_query.lower()
        target_user_id = username_to_id_cache.get(clean_username)

    if not target_user_id:
        await update.message.reply_text(
            f"❌ Could not locate user record for <code>{target_query}</code> in cache.\n"
            f"Please supply their numerical <b>User ID</b> directly if they have not sent a recent message.",
            parse_mode=ParseMode.HTML
        )
        return

    key = (chat.id, target_user_id)
    prev_strikes = user_strikes.pop(key, 0)

    target_name = user_id_to_name_cache.get(target_user_id, target_query)
    await update.message.reply_text(
        f"✅ Successfully reset strikes for <b>{target_name}</b> (<code>{target_user_id}</code>).\n"
        f"Previous strikes: {prev_strikes} ➔ New strikes: 0",
        parse_mode=ParseMode.HTML
    )
    logger.info(f"Admin {user.first_name} reset strikes for user {target_user_id} in {chat.id}")


# ------------------------------------------------------------------------------
# 7. Main Application Runner
# ------------------------------------------------------------------------------
def main():
    if not BOT_TOKEN:
        logger.error("FATAL: BOT_TOKEN environment variable is not set. Please populate .env file.")
        raise ValueError("BOT_TOKEN is required to run this bot.")

    logger.info("Initializing Telegram Bot with python-telegram-bot v20+...")

    application = (
        Application.builder()
        .token(BOT_TOKEN)
        .concurrent_updates(True)
        .build()
    )

    application.add_handler(CallbackQueryHandler(on_verify_callback, pattern="^verify_membership$"))
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("stats", stats_command))
    application.add_handler(CommandHandler("reset_strikes", reset_strikes_command))
    application.add_handler(ChatMemberHandler(chat_member_updated_handler, ChatMemberHandler.CHAT_MEMBER))
    application.add_handler(MessageHandler(filters.StatusUpdate.NEW_CHAT_MEMBERS, message_new_chat_members_handler))

    application.add_handler(
        MessageHandler(
            (filters.TEXT | filters.CAPTION) & ~filters.COMMAND,
            anti_link_filter_handler
        ),
        group=1
    )

    logger.info("Bot handlers registered successfully. Starting long polling...")
    application.run_polling(drop_pending_updates=True)


if __name__ == "__main__":
    main()
