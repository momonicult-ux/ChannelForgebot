"""
bot/handlers.py — Register all Telegram command, conversation, and callback handlers.

v7: Added ConversationHandler for /rewrite, /audit, /boost (first use of
    multi-step text input in this project).
    Added /engage, /autopilot commands.
    ConversationHandlers registered BEFORE the generic CallbackQueryHandler
    to avoid routing conflicts.
v11: Added unknown_message fallback handler with group=1 to treat plain text as /post.
"""

from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ConversationHandler,
    MessageHandler,
    filters,
)

from bot.commands import (
    # Existing
    callback_handler,
    cmd_caption,
    cmd_help,
    cmd_hook,
    cmd_ideas,
    cmd_meme,
    cmd_post,
    cmd_profile,
    cmd_referral,
    cmd_start,
    cmd_stats,
    cmd_trend,
    cmd_trending,
    cmd_upgrade,
    cmd_viral,
    cmd_weekpack,
    # v7: New commands
    cmd_engage,
    cmd_autopilot,
    cmd_autopilot_stop,
    # v7: ConversationHandler states and callbacks
    REWRITE_WAITING,
    cmd_rewrite,
    rewrite_receive_text,
    AUDIT_COLLECTING,
    cmd_audit,
    audit_collect_post,
    audit_done,
    BOOST_WAITING,
    cmd_boost,
    boost_receive_text,
)


async def unknown_message(update, context):
    """Fallback handler: treat plain text as /post request."""
    text = update.message.text.strip()
    context.args = text.split()
    await cmd_post(update, context)


def register_handlers(app: Application) -> None:
    """Attach every command and callback handler to the Application."""

    # ── ConversationHandlers (must be registered before generic callbacks) ──

    # /rewrite — 1 state: wait for pasted text
    rewrite_conv = ConversationHandler(
        entry_points=[CommandHandler("rewrite", cmd_rewrite)],
        states={
            REWRITE_WAITING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, rewrite_receive_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("❌ Cancelled.") or -1)],
        conversation_timeout=300,  # 5 min timeout
    )
    app.add_handler(rewrite_conv)

    # /audit — 1 state: collect forwarded posts, /done triggers analysis
    audit_conv = ConversationHandler(
        entry_points=[CommandHandler("audit", cmd_audit)],
        states={
            AUDIT_COLLECTING: [
                CommandHandler("done", audit_done),
                MessageHandler(filters.TEXT & ~filters.COMMAND, audit_collect_post),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("❌ Audit cancelled.") or -1)],
        conversation_timeout=600,  # 10 min timeout
    )
    app.add_handler(audit_conv)

    # /boost — 1 state: wait for pasted post
    boost_conv = ConversationHandler(
        entry_points=[CommandHandler("boost", cmd_boost)],
        states={
            BOOST_WAITING: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, boost_receive_text),
            ],
        },
        fallbacks=[CommandHandler("cancel", lambda u, c: u.message.reply_text("❌ Cancelled.") or -1)],
        conversation_timeout=300,
    )
    app.add_handler(boost_conv)

    # ── Standard CommandHandlers ─────────────────────────────────────────

    app.add_handler(CommandHandler("start", cmd_start))
    app.add_handler(CommandHandler("help", cmd_help))
    app.add_handler(CommandHandler("post", cmd_post))
    app.add_handler(CommandHandler("meme", cmd_meme))
    app.add_handler(CommandHandler("caption", cmd_caption))
    app.add_handler(CommandHandler("ideas", cmd_ideas))
    app.add_handler(CommandHandler("upgrade", cmd_upgrade))
    app.add_handler(CommandHandler("stats", cmd_stats))
    app.add_handler(CommandHandler("hook", cmd_hook))
    app.add_handler(CommandHandler("trend", cmd_trend))
    app.add_handler(CommandHandler("trending", cmd_trending))
    app.add_handler(CommandHandler("weekpack", cmd_weekpack))
    app.add_handler(CommandHandler("profile", cmd_profile))
    app.add_handler(CommandHandler("viral", cmd_viral))
    app.add_handler(CommandHandler("referral", cmd_referral))
    app.add_handler(CommandHandler("engage", cmd_engage))
    app.add_handler(CommandHandler("autopilot", cmd_autopilot_stop))  # Handles both /autopilot and /autopilot stop

    # ── Generic CallbackQueryHandler (must be LAST in group 0) ───────────
    app.add_handler(CallbackQueryHandler(callback_handler))

    # ── Fallback MessageHandler (group 1: processes after all group 0 handlers) ──
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, unknown_message),
        group=1
    )
