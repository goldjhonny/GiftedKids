import os
import logging
import uuid
from datetime import datetime, timedelta

from telegram import (
    Update,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
)
from telegram.constants import ChatType
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ConversationHandler,
    CallbackQueryHandler,
    ChatJoinRequestHandler,
    ContextTypes,
    filters,
)

# ========= Config from environment =========
BOT_TOKEN = os.getenv("BOT_TOKEN", "").strip()
GROUP_ID_ENV = os.getenv("GROUP_ID", "").strip()
ADMIN_IDS_ENV = os.getenv("ADMIN_IDS", "").strip()

if not BOT_TOKEN:
    raise RuntimeError("Missing BOT_TOKEN env var")
if not GROUP_ID_ENV:
    raise RuntimeError("Missing GROUP_ID env var")
try:
    GROUP_ID = int(GROUP_ID_ENV)
except Exception as e:
    raise RuntimeError("GROUP_ID must be an integer (e.g., -100xxxxxxxxxx)") from e

ADMIN_IDS = []
if ADMIN_IDS_ENV:
    try:
        ADMIN_IDS = [int(x.strip()) for x in ADMIN_IDS_ENV.split(",") if x.strip()]
    except Exception as e:
        raise RuntimeError("ADMIN_IDS must be comma-separated integers") from e
if not ADMIN_IDS:
    raise RuntimeError("Set at least one admin in ADMIN_IDS")

# ========= Logging =========
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
logger = logging.getLogger("join-bot")

# ========= Conversation states =========
Q1_REASON, Q2_RULES, Q3_EXTRA = range(3)

# ========= In-memory stores =========
# applications: app_id -> {
#   'user_id': int,
#   'username': str|None,
#   'answers': dict,
#   'status': 'pending'|'approved'|'rejected',
#   'admin_id': int|None,
#   'admin_messages': dict[admin_id] = (chat_id, message_id),
#   'invite_link': str|None,
# }
applications = {}

# allowed_links: invite_link -> { 'user_id': int, 'app_id': str, 'created_at': datetime }
allowed_links = {}


def is_admin(user_id: int) -> bool:
    return user_id in ADMIN_IDS


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    text = (
        "Welcome! If you want to join the group, use /apply to answer a short questionnaire. "
        "An admin will review your answers. If approved, you’ll receive a special link that "
        "submits a join request, and the bot will approve only you."
    )
    await update.effective_message.reply_text(text)


async def help_cmd(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text(
        "Commands:\n"
        "/apply – Apply to join the group\n"
        "/cancel – Cancel the current application\n"
        "/whereami – Show chat ID (useful in groups)\n"
    )


async def whereami(update: Update, context: ContextTypes.DEFAULT_TYPE):
    chat = update.effective_chat
    await update.effective_message.reply_text(
        f"Chat type: {chat.type}\nChat ID: {chat.id}"
    )


# ===== Conversation: Application =====
async def apply_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_chat.type != ChatType.PRIVATE:
        await update.effective_message.reply_text(
            "Please start a private chat with me first and use /apply there."
        )
        return ConversationHandler.END

    await update.effective_message.reply_text(
        "Thanks for your interest! First question:\n"
        "1) Why do you want to join this group?"
    )
    return Q1_REASON


async def q1_reason(update: Update, context: ContextTypes.DEFAULT_TYPE):
    context.user_data["answers"] = {}
    context.user_data["answers"]["reason"] = update.effective_message.text.strip()

    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Yes, I agree", callback_data="rules_yes"),
                InlineKeyboardButton("No", callback_data="rules_no"),
            ]
        ]
    )
    await update.effective_message.reply_text(
        "2) Do you agree to follow the group rules?",
        reply_markup=kb,
    )
    return Q2_RULES


async def q2_rules(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    await query.answer()
    if query.data == "rules_no":
        await query.edit_message_text(
            "You must agree to the rules to proceed. Application canceled."
        )
        return ConversationHandler.END

    await query.edit_message_text(
        "Great! 3) Anything else you want the admins to know? (Optional). "
        "You can type your message or send '-' to skip."
    )
    return Q3_EXTRA


async def q3_extra(update: Update, context: ContextTypes.DEFAULT_TYPE):
    extra = update.effective_message.text.strip()
    if extra == "-":
        extra = ""
    context.user_data["answers"]["extra"] = extra

    user = update.effective_user
    app_id = str(uuid.uuid4())

    applications[app_id] = {
        "user_id": user.id,
        "username": user.username,
        "answers": context.user_data["answers"].copy(),
        "status": "pending",
        "admin_id": None,
        "admin_messages": {},
        "invite_link": None,
    }

    await update.effective_message.reply_text(
        "Thanks! Your application has been sent to the admins. "
        "If approved, you’ll receive a link here to submit a join request."
    )

    summary = build_application_summary(app_id)
    kb = InlineKeyboardMarkup(
        [
            [
                InlineKeyboardButton("Approve", callback_data=f"APPROVE:{app_id}"),
                InlineKeyboardButton("Reject", callback_data=f"REJECT:{app_id}"),
            ]
        ]
    )

    for admin_id in ADMIN_IDS:
        try:
            msg = await context.bot.send_message(
                chat_id=admin_id,
                text=summary,
                reply_markup=kb,
                disable_web_page_preview=True,
            )
            applications[app_id]["admin_messages"][admin_id] = (msg.chat_id, msg.message_id)
        except Exception as e:
            logger.warning("Failed to send application %s to admin %s: %s", app_id, admin_id, e)

    context.user_data.pop("answers", None)
    return ConversationHandler.END


def build_application_summary(app_id: str) -> str:
    app = applications[app_id]
    user_id = app["user_id"]
    username = app["username"]
    answers = app["answers"]
    uline = f"@{username}" if username else "(no username)"
    text = (
        f"New application #{app_id}\n"
        f"User: {uline} | ID: {user_id}\n\n"
        f"Q1: Why join?\n{answers.get('reason','')}\n\n"
        f"Q2: Agrees to rules: Yes\n\n"
        f"Q3: Extra:\n{answers.get('extra','') or '(none)'}"
    )
    return text


async def cancel(update: Update, context: ContextTypes.DEFAULT_TYPE):
    await update.effective_message.reply_text("Application canceled.")
    context.user_data.pop("answers", None)
    return ConversationHandler.END


# ===== Admin callbacks =====
async def review_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    query = update.callback_query
    user = update.effective_user
    await query.answer()

    if not is_admin(user.id):
        await query.answer("You are not authorized.", show_alert=True)
        return

    try:
        action, app_id = query.data.split(":", 1)
    except Exception:
        await query.answer("Invalid action.", show_alert=True)
        return

    app = applications.get(app_id)
    if not app:
        await query.edit_message_text("This application no longer exists.")
        return

    if app["status"] != "pending":
        await query.edit_message_text(f"Already {app['status']} by admin {app['admin_id']}.")
        return

    if action == "APPROVE":
        await handle_approve(query, context, app_id, app)
    elif action == "REJECT":
        await handle_reject(query, context, app_id, app)
    else:
        await query.answer("Unknown action.", show_alert=True)


async def handle_approve(query, context, app_id, app):
    app["status"] = "approved"
    app["admin_id"] = query.from_user.id

    # Create a join-request invite link valid for 48 hours
    invite_url = None
    try:
        expire_at = datetime.utcnow() + timedelta(hours=48)
        link = await context.bot.create_chat_invite_link(
            chat_id=GROUP_ID,
            name=f"Approval for user {app['user_id']} ({app_id[:8]})",
            expire_date=expire_at,
            creates_join_request=True,
        )
        invite_url = link.invite_link
        app["invite_link"] = invite_url
        allowed_links[invite_url] = {
            "user_id": app["user_id"],
            "app_id": app_id,
            "created_at": datetime.utcnow(),
        }
    except Exception as e:
        logger.exception("Failed to create join-request invite link: %s", e)

    # Notify applicant
    try:
        if invite_url:
            await context.bot.send_message(
                chat_id=app["user_id"],
                text=(
                    "Your application was approved! Use this link to submit a join request "
                    "(valid for 48 hours). The bot will approve only you:\n"
                    f"{invite_url}"
                ),
                disable_web_page_preview=True,
            )
        else:
            await context.bot.send_message(
                chat_id=app["user_id"],
                text=(
                    "Your application was approved, but we couldn't create the invite link "
                    "right now. Please try again later or contact an admin."
                ),
            )
    except Exception as e:
        logger.warning("Failed to notify applicant %s: %s", app["user_id"], e)

    # Update all admin messages for this app
    await update_admin_messages_processed(context, app_id, status="approved")

    await query.edit_message_text(
        f"Application {app_id} approved by {query.from_user.id}"
        + ("" if invite_url else " (but link creation failed)")
    )


async def handle_reject(query, context, app_id, app):
    app["status"] = "rejected"
    app["admin_id"] = query.from_user.id

    try:
        await context.bot.send_message(
            chat_id=app["user_id"],
            text="Sorry, your application was not approved. You can re-apply later.",
        )
    except Exception as e:
        logger.warning("Failed to notify rejected applicant %s: %s", app["user_id"], e)

    await update_admin_messages_processed(context, app_id, status="rejected")
    await query.edit_message_text(f"Application {app_id} rejected by {query.from_user.id}")


async def update_admin_messages_processed(context, app_id: str, status: str):
    app = applications.get(app_id)
    if not app:
        return
    for admin_id, (chat_id, message_id) in list(app["admin_messages"].items()):
        try:
            await context.bot.edit_message_text(
                chat_id=chat_id,
                message_id=message_id,
                text=f"[{status.upper()}] {build_application_summary(app_id)}",
                disable_web_page_preview=True,
            )
        except Exception:
            pass


# ===== Join request handler =====
async def on_join_request(update: Update, context: ContextTypes.DEFAULT_TYPE):
    req = update.chat_join_request
    if not req:
        return

    chat_id = req.chat.id
    user_id = req.from_user.id
    link_str = req.invite_link.invite_link if req.invite_link else None

    # Only handle requests for our configured group
    if chat_id != GROUP_ID:
        return

    # Approve only if the request came through a known per-user link and matches the intended user
    if link_str and link_str in allowed_links:
        entry = allowed_links.get(link_str)
        if entry and entry.get("user_id") == user_id:
            try:
                await context.bot.approve_chat_join_request(chat_id=chat_id, user_id=user_id)
                # Revoke the link so it can't be reused
                try:
                    await context.bot.revoke_chat_invite_link(chat_id=chat_id, invite_link=link_str)
                except Exception as e:
                    logger.warning("Failed to revoke invite link: %s", e)
                # Cleanup
                allowed_links.pop(link_str, None)
                # Optionally mark the application as completed
                app_id = entry.get("app_id")
                if app_id in applications:
                    applications[app_id]["status"] = "approved"  # already approved; could add 'joined'
            except Exception as e:
                logger.exception("Failed to approve join request for %s: %s", user_id, e)
            return

    # Unknown or mismatched request: decline
    try:
        await context.bot.decline_chat_join_request(chat_id=chat_id, user_id=user_id)
    except Exception as e:
        logger.warning("Failed to decline unexpected join request from %s: %s", user_id, e)


def build_application_handler():
    return ConversationHandler(
        entry_points=[CommandHandler("apply", apply_start)],
        states={
            Q1_REASON: [MessageHandler(filters.TEXT & ~filters.COMMAND, q1_reason)],
            Q2_RULES: [CallbackQueryHandler(q2_rules, pattern="^rules_")],
            Q3_EXTRA: [MessageHandler(filters.TEXT & ~filters.COMMAND, q3_extra)],
        },
        fallbacks=[CommandHandler("cancel", cancel)],
        name="apply_conversation",
        persistent=False,
    )


def main():
    app = Application.builder().token(BOT_TOKEN).build()

    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_cmd))
    app.add_handler(CommandHandler("whereami", whereami))
    app.add_handler(build_application_handler())
    app.add_handler(CallbackQueryHandler(review_callback, pattern="^(APPROVE|REJECT):"))
    app.add_handler(ChatJoinRequestHandler(on_join_request))

    logger.info("Bot started. Group ID: %s; Admins: %s", GROUP_ID, ADMIN_IDS)
    logger.info("Ensure the bot is an admin in the group with permissions to invite users and manage join requests.")
    app.run_polling(close_loop=False)


if __name__ == "__main__":
    main()
