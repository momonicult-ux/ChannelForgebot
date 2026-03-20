"""
bot/commands.py — All Telegram command and callback-query handlers.

v6 — Product features:
  Feature 1: Expanded rewrite engine (6 buttons) + Redis session storage
  Feature 2: /start onboarding flow (niche → audience → tone)
  Feature 3: send_daily_ideas() for premium user scheduler
  Feature 4: /trending command
  Feature 5: /viral command (engagement posts)
  Feature 6: Referral system (/start ref_XXX + /referral command)
  Feature 7: Viral CTA button on all generated posts
"""

import asyncio
import logging
import time
from datetime import date

import openai
from telegram import Update, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.error import BadRequest as TelegramBadRequest
from telegram.ext import ContextTypes

from config import (
    AI_CACHE_TTL,
    BOT_USERNAME,
    BRAND_FOOTER,
    COOLDOWN_SECONDS,
    DAILY_LIMIT,
    MAX_MSG_CHARS,
    MAX_TOPIC_CHARS,
    OPENAI_MODEL,
    OWNER_ID,
    REFERRAL_BONUS_REQUESTS,
    TRUNCATION_NOTE,
)
from database.db import (
    check_rate_limit as db_check_rate_limit,
    create_user_if_not_exists,
    get_active_autopilot_users,
    get_autopilot_settings,
    get_command_breakdown,
    get_daily_limit_for_user,
    get_generation_stats,
    get_premium_telegram_ids,
    get_referral_count,
    get_referral_link,
    get_remaining_daily_quota as db_get_remaining_daily_quota,
    get_stats,
    get_user_profile,
    is_new_user,
    is_user_premium,
    record_generation_event,
    record_referral,
    record_usage_event,
    save_autopilot_settings,
    save_channel_audit,
    save_user_profile,
    update_autopilot_delivery,
)
import redis_client
import prompts as prompt_registry
from bot.openai_service import (
    ask_openai,
    OpenAIResult,
    CAPTION_SYSTEM,
    DAILY_IDEAS_SYSTEM,
    ENGAGE_SYSTEM,
    HOOK_REGEN_SYSTEM,
    HOOK_SYSTEM,
    IDEAS_SYSTEM,
    MEME_SYSTEM,
    POST_SYSTEM,
    REWRITE_ANOTHER_SYSTEM,
    REWRITE_CTA_SYSTEM,
    REWRITE_EMOTIONAL_SYSTEM,
    REWRITE_MEME_SYSTEM,
    REWRITE_SHORTER_SYSTEM,
    REWRITE_SMARTER_SYSTEM,
    REWRITE_VIRAL_SYSTEM,
    TREND_SYSTEM,
    VIRAL_SYSTEM,
    WEEKPACK_SYSTEM,
)
from bot.keyboards import (
    autopilot_frequency_keyboard,
    autopilot_mix_keyboard,
    boost_action_keyboard,
    channel_publish_keyboard,
    engage_format_keyboard,
    onboarding_audience_keyboard,
    onboarding_niche_keyboard,
    onboarding_tone_keyboard,
    profile_audience_keyboard,
    profile_niche_keyboard,
    profile_reset_keyboard,
    profile_style_keyboard,
    profile_tone_keyboard,
    regenerate_keyboard,
    rewrite_keyboard,
    viral_cta_keyboard,
)

logger = logging.getLogger(__name__)
_user_last_request: dict[int, float] = {}
_T = asyncio.to_thread


# ─── HELPERS ─────────────────────────────────────────────────────────────────

def _with_branding(text: str) -> str:
    combined = text + BRAND_FOOTER
    if len(combined) <= MAX_MSG_CHARS:
        return combined
    budget = MAX_MSG_CHARS - len(BRAND_FOOTER) - len(TRUNCATION_NOTE)
    return text[:budget].rstrip() + TRUNCATION_NOTE + BRAND_FOOTER


def _rewrite_markup() -> InlineKeyboardMarkup:
    """Rewrite buttons + viral CTA row."""
    rows = rewrite_keyboard().inline_keyboard
    cta_row = viral_cta_keyboard(BOT_USERNAME).inline_keyboard[0]
    return InlineKeyboardMarkup(rows + [cta_row])


async def _send_long_message(update: Update, text: str, reply_markup=None) -> None:
    limit = 4096
    if len(text) <= limit:
        await update.message.reply_text(text, reply_markup=reply_markup)
        return
    chunks: list[str] = []
    remaining = text
    while len(remaining) > limit:
        split_at = remaining.rfind("\n\n", 0, limit)
        if split_at == -1:
            split_at = remaining.rfind("\n", 0, limit)
        if split_at == -1:
            split_at = limit
        chunks.append(remaining[:split_at].rstrip())
        remaining = remaining[split_at:].lstrip("\n")
    if remaining.strip():
        chunks.append(remaining)
    for i, chunk in enumerate(chunks):
        is_last = i == len(chunks) - 1
        await update.message.reply_text(chunk, reply_markup=reply_markup if is_last else None)


def _validate_topic(topic: str) -> str | None:
    if not topic:
        return None
    if len(topic) > MAX_TOPIC_CHARS:
        return f"⚠️ Topic too long ({len(topic)} chars). Max {MAX_TOPIC_CHARS}."
    return None


async def _ensure_user(update: Update) -> bool:
    try:
        u = update.effective_user
        await _T(create_user_if_not_exists, telegram_id=u.id, username=u.username, first_name=u.first_name)
        return True
    except Exception as exc:
        logger.error("DB error in _ensure_user: %s", exc)
        return False


async def _db_error_reply(update: Update) -> None:
    await update.message.reply_text("⚙️ Temporary issue. Please try again in a moment.")


async def _enforce_limits(update: Update) -> bool:
    user_id = update.effective_user.id
    try:
        premium = await _T(is_user_premium, user_id)
    except Exception as exc:
        logger.error("DB premium check error: %s", exc)
        await _db_error_reply(update)
        return False

    if not premium:
        cd = await redis_client.check_cooldown(user_id)
        if cd == -1.0:
            last = _user_last_request.get(user_id)
            if last is not None:
                rem = COOLDOWN_SECONDS - (time.monotonic() - last)
                if rem > 0:
                    await update.message.reply_text(f"⏳ Wait {rem:.1f}s before your next request.")
                    return False
        elif cd > 0:
            await update.message.reply_text(f"⏳ Wait {cd:.1f}s before your next request.")
            return False

    allowed, remaining = await redis_client.check_rate_limit(user_id, premium)
    if remaining == -1:
        try:
            if not await _T(db_check_rate_limit, user_id):
                limit = await _T(get_daily_limit_for_user, user_id)
                await update.message.reply_text(
                    f"🚫 You've reached today's limit of {limit} requests.\n"
                    "💎 Use /upgrade to go Pro, or /referral to earn more!"
                )
                return False
        except Exception as exc:
            logger.error("DB rate limit error: %s", exc)
            await _db_error_reply(update)
            return False
    elif not allowed:
        await update.message.reply_text(
            "🚫 Daily limit reached.\n💎 /upgrade for Pro, or /referral to earn more!"
        )
        return False

    _user_last_request[user_id] = time.monotonic()
    return True


async def _handle_openai_error(exc: Exception, update: Update, command: str) -> None:
    uid = update.effective_user.id
    if isinstance(exc, openai.APITimeoutError):
        await update.message.reply_text("⏱️ AI took too long. Try again shortly.")
    elif isinstance(exc, openai.RateLimitError):
        await update.message.reply_text("⏳ AI at capacity. Wait 30–60s and retry.")
    elif isinstance(exc, openai.AuthenticationError):
        logger.critical("OpenAI auth error: %s", exc)
        await update.message.reply_text("⚙️ Configuration issue. We've been notified.")
    else:
        logger.error("OpenAI error /%s user %s: %s", command, uid, exc)
        await update.message.reply_text(f"❌ Error generating {command}. Please try again.")


def _is_group_or_channel(update: Update) -> bool:
    return update.effective_chat.type in ("group", "supergroup", "channel")


def _extract_topic(ctx: ContextTypes.DEFAULT_TYPE) -> str:
    return " ".join(ctx.args).strip() if ctx.args else ""


async def _safe_record(tid: int, cmd: str) -> None:
    try:
        await _T(record_usage_event, tid, cmd)
    except Exception as exc:
        logger.warning("Usage record failed: %s", exc)


async def _record_gen(tid: int, cmd: str, result: OpenAIResult | None = None, *, error_type: str | None = None, latency_ms: int = 0) -> None:
    try:
        if result:
            await _T(record_generation_event, tid, command=cmd, model=result.model,
                     prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
                     total_tokens=result.total_tokens, estimated_cost_usd=result.estimated_cost_usd,
                     latency_ms=result.latency_ms, status="success")
        else:
            await _T(record_generation_event, tid, command=cmd, model=OPENAI_MODEL,
                     prompt_tokens=0, completion_tokens=0, total_tokens=0,
                     estimated_cost_usd=0.0, latency_ms=latency_ms, status="error", error_type=error_type)
    except Exception as exc:
        logger.warning("Gen record failed: %s", exc)


async def _store_last(uid: int, text: str, ctx: ContextTypes.DEFAULT_TYPE) -> None:
    """Store last AI response in Redis session AND context.user_data as fallback."""
    ctx.user_data["last_ai_response"] = text
    await redis_client.set_session(uid, "last_ai_response", text)


async def _get_last(uid: int, ctx: ContextTypes.DEFAULT_TYPE) -> str | None:
    """Retrieve last AI response from Redis, falling back to context."""
    text = await redis_client.get_session(uid, "last_ai_response")
    if text:
        return text
    return ctx.user_data.get("last_ai_response")


# ─── /start WITH ONBOARDING + REFERRAL (Features 2, 6) ──────────────────────

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_user(update):
        await _db_error_reply(update)
        return
    user_id = update.effective_user.id
    name = update.effective_user.first_name or "there"

    # ── Referral deep-link: /start ref_123456 ──
    args = _extract_topic(context)
    if args.startswith("ref_"):
        try:
            referrer_tid = int(args[4:])
            if referrer_tid != user_id:
                created = await _T(record_referral, referrer_tid, user_id)
                if created:
                    logger.info("Referral recorded: %s referred by %s", user_id, referrer_tid)
        except (ValueError, Exception) as exc:
            logger.warning("Referral parse error: %s", exc)

    # ── Check if new user → trigger onboarding ──
    new = await _T(is_new_user, user_id)

    premium = await _T(is_user_premium, user_id)
    tier = (
        "✨ You're on Pro — unlimited requests!\n"
        if premium else (
            f"You have {DAILY_LIMIT} free requests/day. Let's use them well 🔥\n"
            "Upgrade anytime with /upgrade."
        )
    )

    await update.message.reply_text(
        f"👋 Hey {name}, welcome to ChannelForgeBot!\n\n"
        "I'm your AI content engine for Telegram channels.\n"
        "Give me a topic — I'll give you a post ready to publish. 🚀\n\n"
        "📌 Quick start:\n"
        "  /post bitcoin crash\n"
        "  /meme crypto winter\n"
        "  /viral AI takeover\n"
        "  /trending\n\n"
        "Type /help for all commands.\n\n" + tier
    )

    # Auto-trigger profile setup for new users
    if new:
        await update.message.reply_text(
            "👤 Let's personalize your experience!\n\n"
            "What is your channel niche?",
            reply_markup=onboarding_niche_keyboard(),
        )

    logger.info("User %s triggered /start (new=%s)", user_id, new)


# ─── /help ───────────────────────────────────────────────────────────────────

async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_user(update):
        await _db_error_reply(update)
        return
    uid = update.effective_user.id
    premium = await _T(is_user_premium, uid)
    remaining = await redis_client.get_remaining_quota(uid, premium)
    if remaining == "?":
        remaining = await _T(db_get_remaining_daily_quota, uid)
    limit = await _T(get_daily_limit_for_user, uid) if not premium else "∞"

    quota = (
        "✨ Pro — unlimited requests, no cooldowns.\n"
        if premium else (
            f"📊 Quota: {remaining}/{limit} requests today.\n"
            "Upgrade with /upgrade or earn more with /referral."
        )
    )
    await update.message.reply_text(
        "🤖 ChannelForgeBot — Commands\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "📝 /post <topic>     — Full channel post\n"
        "😂 /meme <topic>     — Meme-style caption\n"
        "✍️ /caption <topic>  — Short punchy caption\n"
        "💡 /ideas            — 5 trending ideas\n"
        "🪝 /hook <topic>     — 10 viral hooks\n"
        "📈 /trending         — 5 trending topics\n"
        "🔥 /viral <topic>    — Viral engagement post\n"
        "💬 /engage <topic>   — Polls, debates, hot takes\n"
        "✏️ /rewrite          — Paste text → AI rewrites\n"
        "🚀 /boost            — Paste text → score + improve\n"
        "🔍 /audit            — Forward posts → channel audit\n"
        "🤖 /autopilot        — Auto-generate daily content\n"
        "📦 /weekpack <niche> — Weekly content pack\n"
        "👤 /profile          — Channel profile\n"
        "🔗 /referral         — Earn extra requests\n"
        "🚀 /upgrade          — Remove all limits\n\n"
        "━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        + quota + "\n"
        "Free-tier resets daily at midnight UTC. 🕛"
    )


# ─── CONTENT PIPELINE ────────────────────────────────────────────────────────

async def _generate_content(
    update: Update, context: ContextTypes.DEFAULT_TYPE, *,
    command: str, system_prompt: str, user_prompt: str,
    max_tokens: int = 450, has_rewrite: bool = True,
    cache_ttl: int = 0,
    session_writes: list[dict] | None = None,
    keyboard: str | None = None,
    delivery: str = "edit",
) -> None:
    """
    Unified content generation pipeline.

    v9: Enqueues jobs to the Redis worker queue for background processing.
    The handler returns immediately after sending a "⚡ Generating..." placeholder.
    The worker edits the placeholder when the result is ready.

    v10: Added session_writes and keyboard parameters for advanced worker features.
    v10: Added delivery parameter to control how worker delivers results.

    Falls back to inline execution when:
    - Redis is unavailable (no queue)
    - The message is in a group/channel (needs context.chat_data for publish flow)
    """
    uid = update.effective_user.id
    profile = await _T(get_user_profile, uid)

    # ── Group/channel mode: always inline (needs chat_data for publish) ──
    if _is_group_or_channel(update):
        await update.message.chat.send_action(ChatAction.TYPING)
        t0 = time.monotonic()
        try:
            result = await ask_openai(system_prompt, user_prompt, max_tokens=max_tokens, profile=profile, cache_ttl=cache_ttl)
            await _safe_record(uid, command)
            await _record_gen(uid, command, result)
            branded = _with_branding(result.text)
            context.chat_data["pending_post"] = branded
            await update.message.reply_text(f"📋 Preview:\n\n{branded}", reply_markup=channel_publish_keyboard())
        except Exception as exc:
            ms = int((time.monotonic() - t0) * 1000)
            await _record_gen(uid, command, error_type=type(exc).__name__, latency_ms=ms)
            await _handle_openai_error(exc, update, command)
        return

    # ── Try enqueue to background worker ─────────────────────────────────
    if redis_client.queue_available():
        import json as _json

        # Send placeholder that worker will edit later
        placeholder = await update.message.reply_text("⚡ Generating your content...")

        job = _json.dumps({
            "chat_id": placeholder.chat_id,
            "message_id": placeholder.message_id,
            "user_id": uid,
            "command": command,
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "max_tokens": max_tokens,
            "profile": profile,
            "cache_ttl": cache_ttl,
            "has_rewrite": has_rewrite,
            "delivery": delivery,
            **({"session_writes": session_writes} if session_writes else {}),
            **({"keyboard": keyboard} if keyboard else {}),
        })

        enqueued = await redis_client.enqueue_job(job)
        if enqueued:
            logger.info("Job enqueued: user=%s cmd=%s", uid, command)
            return  # Handler is done — worker will deliver the result

        # Enqueue failed — fall through to inline execution
        logger.warning("Queue enqueue failed for user %s — falling back to inline.", uid)
        # Delete the placeholder since we'll send a new reply inline
        try:
            await placeholder.delete()
        except Exception:
            pass

    # ── Queue unavailable ─────────────────────────────────────────────────
    await update.message.reply_text(
        "⚠️ The AI system is temporarily unavailable. Please try again in a moment."
    )


async def cmd_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_user(update):
        return await _db_error_reply(update)
    topic = _extract_topic(context)
    if not topic:
        return await update.message.reply_text("⚠️ Provide a topic.\nExample: /post bitcoin crash")
    err = _validate_topic(topic)
    if err:
        return await update.message.reply_text(err)
    if not await _enforce_limits(update):
        return
    logger.info("User %s /post %r", update.effective_user.id, topic)
    await _generate_content(update, context, command="post", system_prompt=POST_SYSTEM,
                            user_prompt=f"Write a Telegram channel post about: {topic}",
                            cache_ttl=AI_CACHE_TTL)


async def cmd_meme(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_user(update):
        return await _db_error_reply(update)
    topic = _extract_topic(context)
    if not topic:
        return await update.message.reply_text("⚠️ Provide a topic.\nExample: /meme crypto crash")
    err = _validate_topic(topic)
    if err:
        return await update.message.reply_text(err)
    if not await _enforce_limits(update):
        return
    await _generate_content(update, context, command="meme", system_prompt=MEME_SYSTEM,
                            user_prompt=f"Create a meme-style Telegram caption about: {topic}",
                            cache_ttl=AI_CACHE_TTL)


async def cmd_caption(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_user(update):
        return await _db_error_reply(update)
    topic = _extract_topic(context)
    if not topic:
        return await update.message.reply_text("⚠️ Provide a topic.\nExample: /caption AI is taking over")
    err = _validate_topic(topic)
    if err:
        return await update.message.reply_text(err)
    if not await _enforce_limits(update):
        return
    await _generate_content(update, context, command="caption", system_prompt=CAPTION_SYSTEM,
                            user_prompt=f"Write a caption for: {topic}", max_tokens=120,
                            cache_ttl=AI_CACHE_TTL)


async def cmd_ideas(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_user(update):
        return await _db_error_reply(update)
    if not await _enforce_limits(update):
        return
    await _generate_content(
        update, context,
        command="ideas",
        system_prompt=IDEAS_SYSTEM,
        user_prompt="Give me 5 trending Telegram post ideas.",
        max_tokens=200,
        has_rewrite=False,
        cache_ttl=AI_CACHE_TTL // 2,
    )


# ─── /viral (Feature 5) ─────────────────────────────────────────────────────

async def cmd_viral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/viral <topic> — Generate a viral engagement post."""
    if not await _ensure_user(update):
        return await _db_error_reply(update)
    topic = _extract_topic(context)
    if not topic:
        profile = await _T(get_user_profile, update.effective_user.id)
        if profile and profile.get("niche"):
            topic = profile["niche"]
        else:
            return await update.message.reply_text("⚠️ Provide a topic.\nExample: /viral AI takeover")
    err = _validate_topic(topic)
    if err:
        return await update.message.reply_text(err)
    if not await _enforce_limits(update):
        return
    logger.info("User %s /viral %r", update.effective_user.id, topic)
    await _generate_content(update, context, command="viral", system_prompt=VIRAL_SYSTEM,
                            user_prompt=f"Create a viral engagement Telegram post about: {topic}",
                            cache_ttl=AI_CACHE_TTL)


# ─── /trending (Feature 4) ──────────────────────────────────────────────────

async def cmd_trending(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/trending — 5 trending topics for the user's niche."""
    if not await _ensure_user(update):
        return await _db_error_reply(update)
    uid = update.effective_user.id
    profile = await _T(get_user_profile, uid)
    niche = _extract_topic(context)
    if not niche:
        if profile and profile.get("niche"):
            niche = profile["niche"]
        else:
            return await update.message.reply_text("⚠️ Provide a niche or set one with /profile.\nExample: /trending crypto")
    if not await _enforce_limits(update):
        return
    logger.info("User %s /trending %r", uid, niche)
    await update.message.chat.send_action(ChatAction.TYPING)
    t0 = time.monotonic()
    try:
        result = await ask_openai(TREND_SYSTEM,
                                  f"Generate 5 trending Telegram discussion topics for: {niche}",
                                  max_tokens=250, profile=profile, cache_ttl=AI_CACHE_TTL // 2)
        await _safe_record(uid, "trending")
        await _record_gen(uid, "trending", result)
        context.user_data["last_trend_niche"] = niche
        await update.message.reply_text(_with_branding(result.text), reply_markup=regenerate_keyboard("trend"))
    except Exception as exc:
        await _record_gen(uid, "trending", error_type=type(exc).__name__, latency_ms=int((time.monotonic()-t0)*1000))
        await _handle_openai_error(exc, update, "trending")


# ─── /referral (Feature 6) ───────────────────────────────────────────────────

async def cmd_referral(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/referral — Show referral link and stats."""
    if not await _ensure_user(update):
        return await _db_error_reply(update)
    uid = update.effective_user.id
    count = await _T(get_referral_count, uid)
    link = get_referral_link(uid, BOT_USERNAME)
    bonus = count * REFERRAL_BONUS_REQUESTS
    limit = await _T(get_daily_limit_for_user, uid)

    await update.message.reply_text(
        "🔗 Your Referral Link\n"
        "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        f"{link}\n\n"
        f"👥 Friends referred: {count}\n"
        f"🎁 Bonus requests:   +{bonus}/day\n"
        f"📊 Your daily limit:  {limit} requests\n\n"
        f"Each referral adds +{REFERRAL_BONUS_REQUESTS} daily requests.\n"
        "Share your link to unlock more! 🚀"
    )


# ─── Existing commands (preserved) ──────────────────────────────────────────

async def cmd_upgrade(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_user(update):
        return await _db_error_reply(update)
    uid = update.effective_user.id
    if await _T(is_user_premium, uid):
        return await update.message.reply_text("✨ You're already on ChannelForge Pro! 🚀")
    await update.message.reply_text(
        "🚀 ChannelForge Pro\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
        "✅ Unlimited AI posts\n✅ Unlimited memes & captions\n"
        "✅ Weekly content packs\n✅ Daily content ideas\n"
        "✅ No cooldown\n✅ Personalised prompts\n\n"
        "💰 $8 / month\n\n━━━━━━━━━━━━━━━━━━━━━━━\n"
        "To subscribe: @ChannelForgeSup\nhttps://channelforgebot.com/pro"
    )


async def cmd_stats(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_user(update):
        return await _db_error_reply(update)
    uid = update.effective_user.id
    if OWNER_ID == 0:
        return await update.message.reply_text("⚠️ Set OWNER_TELEGRAM_ID in .env.")
    if uid != OWNER_ID:
        return await update.message.reply_text("❌ Restricted to bot owner.")
    try:
        s = await _T(get_stats)
        bd = await _T(get_command_breakdown)
        gen = await _T(get_generation_stats)
        cache = await redis_client.get_cache_stats()
        queue_depth = await redis_client.get_queue_depth()
        from workers.ai_worker import get_worker_stats
        wk = get_worker_stats()
    except Exception:
        return await _db_error_reply(update)
    today = date.today().isoformat()
    bds = "\n".join(f"  /{c}: {n}" for c, n in sorted(bd.items(), key=lambda x: x[1], reverse=True)) if bd else "  (none)"
    await update.message.reply_text(
        f"📊 Stats ({today})\n━━━━━━━━━━━━━━━━━━━━━━━━━\n"
        f"Users: {s['today_active_users']} active / {s['total_users']} total\n"
        f"Requests: {s['today_requests']} today / {s['total_requests']} total\n"
        f"Gens: {gen['today_generations']} today, {gen['today_tokens']:,} tokens, ${gen['today_cost']:.4f}\n"
        f"Avg latency: {gen['avg_latency_ms']:.0f}ms\n"
        f"Total cost: ${gen['total_cost']:.4f}\n"
        f"Cache: {cache['cached_responses']} cached\n"
        f"Queue: {queue_depth} pending | {wk['jobs_processed']} done | {wk['jobs_failed']} failed\n"
        f"Premium: {s['premium_users']} | Referrals: {s.get('total_referrals', 0)}\n\n{bds}"
    )


async def cmd_hook(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_user(update):
        return await _db_error_reply(update)
    topic = _extract_topic(context)
    if not topic:
        return await update.message.reply_text("⚠️ Provide a topic.\nExample: /hook bitcoin ETF")
    if _validate_topic(topic):
        return await update.message.reply_text(_validate_topic(topic))
    if not await _enforce_limits(update):
        return
    await _generate_content(
        update, context,
        command="hook",
        system_prompt=HOOK_SYSTEM,
        user_prompt=f"Generate 10 viral hooks about: {topic}",
        max_tokens=350,
        has_rewrite=False,
        cache_ttl=AI_CACHE_TTL,
        session_writes=[
            {"key": "last_hook_topic", "value": topic},
            {"key": "last_hook_output", "value": "$RESULT"},
        ],
        keyboard="regenerate:hook",
    )


async def cmd_trend(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_user(update):
        return await _db_error_reply(update)
    topic = _extract_topic(context)
    if not topic:
        # Fallback to profile niche if no topic provided
        uid = update.effective_user.id
        profile = await _T(get_user_profile, uid)
        if profile and profile.get("niche"):
            topic = profile["niche"]
        else:
            return await update.message.reply_text("⚠️ Provide a niche or set with /profile.")
    if not await _enforce_limits(update):
        return
    await _generate_content(
        update, context,
        command="trend",
        system_prompt=TREND_SYSTEM,
        user_prompt=f"Generate 5 trending topics for: {topic}",
        max_tokens=250,
        has_rewrite=False,
        cache_ttl=AI_CACHE_TTL // 2,
        session_writes=[
            {"key": "last_trend_niche", "value": topic},
        ],
        keyboard="regenerate:trend",
    )


async def cmd_weekpack(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_user(update):
        return await _db_error_reply(update)
    if not await _enforce_limits(update):
        return
    # Extract niche from command or fallback to profile
    niche = _extract_topic(context)
    if not niche:
        uid = update.effective_user.id
        profile = await _T(get_user_profile, uid)
        if profile and profile.get("niche"):
            niche = profile["niche"]
    hint = f" for a {niche} channel" if niche else ""
    await _generate_content(
        update, context,
        command="weekpack",
        system_prompt=WEEKPACK_SYSTEM,
        user_prompt=f"Generate a weekly content pack{hint}.",
        max_tokens=1500,
        has_rewrite=False,
        cache_ttl=0,
        delivery="send_long",
    )


async def cmd_profile(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not await _ensure_user(update):
        return await _db_error_reply(update)
    uid = update.effective_user.id
    profile = await _T(get_user_profile, uid)
    if profile and any(profile.values()):
        await update.message.reply_text(
            f"👤 Your Profile\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Niche:    {profile.get('niche') or '—'}\n"
            f"Audience: {profile.get('audience') or '—'}\n"
            f"Tone:     {profile.get('tone') or '—'}\n"
            f"Style:    {profile.get('post_style') or '—'}\n\n"
            "This profile personalises all AI output.\nUpdate it?",
            reply_markup=profile_reset_keyboard(),
        )
    else:
        await update.message.reply_text(
            "👤 Set up your channel profile!\nFirst, choose your niche:",
            reply_markup=profile_niche_keyboard(),
        )


# ─── DAILY IDEAS SENDER (Feature 3) ─────────────────────────────────────────

async def send_daily_ideas(bot) -> None:
    """
    Called by the scheduler in main.py.  Sends daily content ideas
    to all premium users based on their profile niche.
    """
    try:
        premium_ids = await _T(get_premium_telegram_ids)
    except Exception as exc:
        logger.error("Daily ideas: failed to get premium users: %s", exc)
        return

    for tid in premium_ids:
        try:
            profile = await _T(get_user_profile, tid)
            niche = (profile.get("niche") if profile else None) or "general"
            result = await ask_openai(
                DAILY_IDEAS_SYSTEM,
                f"Generate 3 daily content ideas for a {niche} Telegram channel.",
                max_tokens=200, profile=profile,
            )
            await bot.send_message(chat_id=tid, text=_with_branding(result.text))
            await _safe_record(tid, "daily_ideas")
            await _record_gen(tid, "daily_ideas", result)
            logger.info("Daily ideas sent to user %s", tid)
        except Exception as exc:
            logger.warning("Daily ideas failed for user %s: %s", tid, exc)

        await asyncio.sleep(0.5)  # Avoid Telegram rate limits


# ─── CALLBACK HANDLER ────────────────────────────────────────────────────────

async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not query or not query.data:
        return
    try:
        await query.answer()
    except Exception:
        pass

    data = query.data
    try:
        if data.startswith("rewrite:"):
            await _handle_rewrite(update, context, data)
        elif data.startswith("regen:"):
            await _handle_regenerate(update, context, data)
        elif data.startswith("channel:"):
            await _handle_channel_action(update, context, data)
        elif data.startswith("profile:"):
            await _handle_profile_step(update, context, data)
        elif data.startswith("onboard:"):
            await _handle_onboarding(update, context, data)
        elif data.startswith("engage:"):
            await _handle_engage_format(update, context, data)
        elif data.startswith("boost:"):
            await _handle_boost_action(update, context, data)
        elif data.startswith("autopilot:"):
            await _handle_autopilot_step(update, context, data)
    except TelegramBadRequest as exc:
        if "not modified" not in str(exc).lower():
            logger.warning("BadRequest: %s", exc)
    except Exception as exc:
        logger.error("Callback error: %s", exc, exc_info=True)


# ─── REWRITE HANDLER (Feature 1: 6 buttons + Redis session) ─────────────────

async def _handle_rewrite(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    uid = query.from_user.id

    last_text = await _get_last(uid, context)
    if not last_text:
        return await query.edit_message_text("⚠️ No post to rewrite. Generate one first!")

    action = data.split(":")[1]
    system_map = {
        "shorter": REWRITE_SHORTER_SYSTEM, "viral": REWRITE_VIRAL_SYSTEM,
        "cta": REWRITE_CTA_SYSTEM, "another": REWRITE_ANOTHER_SYSTEM,
        "emotional": REWRITE_EMOTIONAL_SYSTEM, "smarter": REWRITE_SMARTER_SYSTEM,
        "meme": REWRITE_MEME_SYSTEM,
    }
    system = system_map.get(action)
    if not system:
        return

    profile = await _T(get_user_profile, uid)

    # Try enqueue to worker queue
    if redis_client.queue_available():
        import json as _json

        # Edit message to placeholder
        try:
            await query.edit_message_text("⚡ Rewriting...")
        except Exception:
            pass

        job = _json.dumps({
            "chat_id": query.message.chat_id,
            "message_id": query.message.message_id,
            "user_id": uid,
            "command": f"rewrite_{action}",
            "system_prompt": system,
            "user_prompt": last_text,
            "max_tokens": 500,
            "profile": profile,
            "cache_ttl": 0,
            "has_rewrite": True,
            "delivery": "edit",
            "keyboard": "rewrite",
            "session_writes": [
                {"key": "last_ai_response", "value": "$RESULT"},
            ],
        })

        enqueued = await redis_client.enqueue_job(job)
        if enqueued:
            logger.info("Rewrite job enqueued: user=%s action=%s", uid, action)
            return  # Worker will deliver the result

        # Enqueue failed - fall through to inline execution
        logger.warning("Rewrite queue enqueue failed for user %s - falling back to inline.", uid)

    # Queue unavailable
    await query.edit_message_text(
        "⚠️ The AI system is temporarily unavailable. Please try again in a moment."
    )


# ─── REGENERATE HANDLER ─────────────────────────────────────────────────────

async def _handle_regenerate(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    uid = query.from_user.id
    cmd = data.split(":")[1]
    profile = await _T(get_user_profile, uid)

    # Prepare command-specific data
    if cmd == "hook":
        # Read from Redis first, fall back to context.user_data
        topic = await redis_client.get_session(uid, "last_hook_topic")
        if not topic:
            topic = context.user_data.get("last_hook_topic", "")
        if not topic:
            return await query.edit_message_text("⚠️ Topic expired. Use /hook again.")
        prev = await redis_client.get_session(uid, "last_hook_output")
        if not prev:
            prev = context.user_data.get("last_hook_output", "")
        
        system_prompt = HOOK_REGEN_SYSTEM
        user_prompt = f"Topic: {topic}\n\nPrevious:\n{prev}"
        max_tokens = 350
        keyboard = "regenerate:hook"
        session_writes = [
            {"key": "last_hook_output", "value": "$RESULT"},
        ]
    elif cmd == "trend":
        # Read from Redis first, fall back to context.user_data
        niche = await redis_client.get_session(uid, "last_trend_niche")
        if not niche:
            niche = context.user_data.get("last_trend_niche", "")
        if not niche:
            return await query.edit_message_text("⚠️ Niche expired. Use /trend again.")
        
        system_prompt = TREND_SYSTEM
        user_prompt = f"5 NEW trending topics for: {niche}"
        max_tokens = 250
        keyboard = "regenerate:trend"
        session_writes = []  # No special session writes for trend
    else:
        return

    # ── Try enqueue to worker queue ───────────────────────────────────────
    if redis_client.queue_available():
        import json as _json

        # Edit message to placeholder
        try:
            await query.edit_message_text("⚡ Generating new batch...")
        except Exception:
            pass

        job = _json.dumps({
            "chat_id": query.message.chat_id,
            "message_id": query.message.message_id,
            "user_id": uid,
            "command": f"{cmd}_regen",
            "system_prompt": system_prompt,
            "user_prompt": user_prompt,
            "max_tokens": max_tokens,
            "profile": profile,
            "cache_ttl": 0,
            "has_rewrite": False,
            "delivery": "edit",
            "keyboard": keyboard,
            "session_writes": session_writes,
        })

        enqueued = await redis_client.enqueue_job(job)
        if enqueued:
            logger.info("Regenerate job enqueued: user=%s cmd=%s", uid, cmd)
            return  # Worker will deliver the result

        # Enqueue failed - fall through to inline execution
        logger.warning("Regenerate enqueue failed for user %s - falling back to inline.", uid)

    # Queue unavailable
    await query.edit_message_text(
        "⚠️ The AI system is temporarily unavailable. Please try again in a moment."
    )


# ─── CHANNEL ACTIONS ─────────────────────────────────────────────────────────

async def _handle_channel_action(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    action = data.split(":")[1]
    pending = context.chat_data.get("pending_post")
    if action == "publish" and pending:
        await query.message.chat.send_message(pending)
        await query.edit_message_text("✅ Published!")
        context.chat_data.pop("pending_post", None)
    elif action == "rewrite" and pending:
        uid = query.from_user.id
        profile = await _T(get_user_profile, uid)
        await query.message.chat.send_action(ChatAction.TYPING)
        try:
            result = await ask_openai(REWRITE_ANOTHER_SYSTEM, pending, max_tokens=500, profile=profile)
            branded = _with_branding(result.text)
            context.chat_data["pending_post"] = branded
            await query.edit_message_text(f"📋 Preview:\n\n{branded}", reply_markup=channel_publish_keyboard())
        except Exception:
            await query.edit_message_text("❌ Rewrite failed.")
    elif action == "cancel":
        context.chat_data.pop("pending_post", None)
        await query.edit_message_text("❌ Cancelled.")


# ─── ONBOARDING HANDLER (Feature 2) ─────────────────────────────────────────

async def _handle_onboarding(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    """Step-by-step onboarding: niche → audience → tone → save profile."""
    query = update.callback_query
    uid = query.from_user.id
    parts = data.split(":", 2)
    field = parts[1]
    value = parts[2] if len(parts) > 2 else ""

    if "onboard_draft" not in context.user_data:
        context.user_data["onboard_draft"] = {}
    draft = context.user_data["onboard_draft"]

    if field == "niche":
        draft["niche"] = value
        await query.edit_message_text(f"✅ Niche: {value}\n\nAudience level?", reply_markup=onboarding_audience_keyboard())
    elif field == "audience":
        draft["audience"] = value
        await query.edit_message_text(f"✅ Audience: {value}\n\nPreferred tone?", reply_markup=onboarding_tone_keyboard())
    elif field == "tone":
        draft["tone"] = value
        await _T(save_user_profile, uid, niche=draft.get("niche", ""), audience=draft.get("audience", ""),
                 tone=draft.get("tone", ""), post_style="")
        context.user_data.pop("onboard_draft", None)
        await query.edit_message_text(
            "✅ Profile set up!\n\n"
            f"Niche: {draft.get('niche', '—')}\n"
            f"Audience: {draft.get('audience', '—')}\n"
            f"Tone: {value}\n\n"
            "All AI content will be personalised for you. 🎯\n"
            "Try /post or /viral to get started!"
        )
        logger.info("User %s completed onboarding: %s", uid, draft)


# ─── PROFILE STEPS (existing) ───────────────────────────────────────────────

async def _handle_profile_step(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    query = update.callback_query
    uid = query.from_user.id
    parts = data.split(":", 2)
    field, value = parts[1], parts[2] if len(parts) > 2 else ""

    if field == "reset":
        if value == "confirm":
            context.user_data["profile_draft"] = {}
            await query.edit_message_text("🔄 Choose your niche:", reply_markup=profile_niche_keyboard())
        else:
            await query.edit_message_text("✅ Profile kept.")
        return

    if "profile_draft" not in context.user_data:
        context.user_data["profile_draft"] = {}
    d = context.user_data["profile_draft"]

    if field == "niche":
        d["niche"] = value
        await query.edit_message_text(f"✅ Niche: {value}\n\nAudience:", reply_markup=profile_audience_keyboard())
    elif field == "audience":
        d["audience"] = value
        await query.edit_message_text(f"✅ Audience: {value}\n\nTone:", reply_markup=profile_tone_keyboard())
    elif field == "tone":
        d["tone"] = value
        await query.edit_message_text(f"✅ Tone: {value}\n\nPost style:", reply_markup=profile_style_keyboard())
    elif field == "style":
        d["post_style"] = value
        await _T(save_user_profile, uid, niche=d.get("niche", ""), audience=d.get("audience", ""),
                 tone=d.get("tone", ""), post_style=d.get("post_style", ""))
        context.user_data.pop("profile_draft", None)
        await query.edit_message_text(
            f"✅ Saved!\n\nNiche: {d.get('niche','—')}\nAudience: {d.get('audience','—')}\n"
            f"Tone: {d.get('tone','—')}\nStyle: {value}\n\nAll content is now personalised. 🎯"
        )


# ═════════════════════════════════════════════════════════════════════════════
# v7 — NEW COMMANDS
# ═════════════════════════════════════════════════════════════════════════════

# ─── /rewrite — Paste text → 4 AI rewrites ──────────────────────────────────
# Uses ConversationHandler (registered in handlers.py).
# State REWRITE_WAITING = 1

REWRITE_WAITING = 1  # ConversationHandler state


async def cmd_rewrite(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/rewrite — Start the rewrite flow. Asks user to paste text."""
    if not await _ensure_user(update):
        await _db_error_reply(update)
        return -1  # ConversationHandler.END
    await update.message.reply_text(
        "✏️ Paste the post you want to rewrite.\n\n"
        "I'll generate 4 improved versions:\n"
        "🔥 Viral  ⚡ Short  😂 Meme  🧠 Smart"
    )
    return REWRITE_WAITING


async def rewrite_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User pasted their text — generate 4 rewrites."""
    if not await _enforce_limits(update):
        return -1

    uid = update.effective_user.id
    original = update.message.text.strip()
    if not original or len(original) < 10:
        await update.message.reply_text("⚠️ Please paste a real post (at least 10 characters).")
        return REWRITE_WAITING

    profile = await _T(get_user_profile, uid)

    # ── Try enqueue to worker queue (multi-call) ─────────────────────────
    if redis_client.queue_available():
        import json as _json

        # Send placeholder that worker will delete and replace
        placeholder = await update.message.reply_text("⚡ Generating rewrites...")

        job = _json.dumps({
            "chat_id": placeholder.chat_id,
            "message_id": placeholder.message_id,
            "user_id": uid,
            "command": "rewrite",
            "profile": profile,
            "cache_ttl": 0,
            "has_rewrite": True,
            "delivery": "send_long",
            "keyboard": "rewrite",
            "session_writes": [
                {"key": "last_ai_response", "value": "$RESULT"},
            ],
            "calls": [
                {
                    "system_prompt": REWRITE_VIRAL_SYSTEM,
                    "user_prompt": original,
                    "max_tokens": 400,
                    "label": "🔥 Viral Version",
                },
                {
                    "system_prompt": REWRITE_SHORTER_SYSTEM,
                    "user_prompt": original,
                    "max_tokens": 300,
                    "label": "⚡ Short Version",
                },
                {
                    "system_prompt": REWRITE_MEME_SYSTEM,
                    "user_prompt": original,
                    "max_tokens": 400,
                    "label": "😂 Meme Version",
                },
                {
                    "system_prompt": REWRITE_SMARTER_SYSTEM,
                    "user_prompt": original,
                    "max_tokens": 400,
                    "label": "🧠 Smart Version",
                },
            ],
        })

        enqueued = await redis_client.enqueue_job(job)
        if enqueued:
            logger.info("Multi-rewrite job enqueued: user=%s", uid)
            return -1  # Worker will deliver result

        # Enqueue failed - fall through to inline execution
        logger.warning("Multi-rewrite enqueue failed for user %s - falling back to inline.", uid)
        try:
            await placeholder.delete()
        except Exception:
            pass

    # Queue unavailable
    await update.message.reply_text(
        "⚠️ The AI system is temporarily unavailable. Please try again in a moment."
    )

    return -1  # ConversationHandler.END


# ─── /engage — Engagement post with format selection ─────────────────────────

async def cmd_engage(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/engage [topic] — Generate an engagement post."""
    if not await _ensure_user(update):
        return await _db_error_reply(update)
    topic = _extract_topic(context)

    if topic:
        # Direct generation with auto-selected format
        err = _validate_topic(topic)
        if err:
            return await update.message.reply_text(err)
        if not await _enforce_limits(update):
            return
        await _generate_content(
            update, context, command="engage",
            system_prompt=ENGAGE_SYSTEM,
            user_prompt=f"Create a high-engagement Telegram post about: {topic}",
            cache_ttl=AI_CACHE_TTL,
        )
    else:
        # No topic — show format picker, store nothing yet
        context.user_data["engage_pending"] = True
        await update.message.reply_text(
            "💬 What type of engagement post?\n\nPick a format:",
            reply_markup=engage_format_keyboard(),
        )


async def _handle_engage_format(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    """User picked an engagement format from the keyboard."""
    query = update.callback_query
    uid = query.from_user.id
    fmt = data.split(":")[1]

    format_labels = {
        "poll": "POLL", "debate": "DEBATE", "hot_take": "HOT TAKE",
        "prediction": "PREDICTION", "fill_blank": "FILL-IN-BLANK", "surprise": None,
    }
    chosen = format_labels.get(fmt)
    profile = await _T(get_user_profile, uid)
    niche = (profile.get("niche") if profile else None) or "general"

    if chosen:
        user_prompt = f"Create a {chosen}-format engagement post for a {niche} Telegram channel."
    else:
        user_prompt = f"Create a high-engagement post (pick best format) for a {niche} channel."

    await query.message.chat.send_action(ChatAction.TYPING)
    t0 = time.monotonic()
    try:
        result = await ask_openai(ENGAGE_SYSTEM, user_prompt, max_tokens=400, profile=profile, cache_ttl=AI_CACHE_TTL // 2)
        await _safe_record(uid, "engage")
        await _record_gen(uid, "engage", result)
        await _store_last(uid, result.text, context)
        await query.edit_message_text(_with_branding(result.text), reply_markup=_rewrite_markup())
    except Exception as exc:
        ms = int((time.monotonic() - t0) * 1000)
        await _record_gen(uid, "engage", error_type=type(exc).__name__, latency_ms=ms)
        await query.edit_message_text("❌ Generation failed. Try /engage <topic> directly.")


# ─── /audit — Forward posts → AI channel audit ──────────────────────────────
# Uses ConversationHandler. State AUDIT_COLLECTING = 2

AUDIT_COLLECTING = 2


async def cmd_audit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/audit — Start collecting posts for a channel audit."""
    if not await _ensure_user(update):
        await _db_error_reply(update)
        return -1
    if not await _enforce_limits(update):
        return -1

    context.user_data["audit_posts"] = []
    await update.message.reply_text(
        "🔍 Channel Audit\n\n"
        "Forward 3–10 posts from your channel.\n"
        "When done, send /done to get your audit report."
    )
    return AUDIT_COLLECTING


async def audit_collect_post(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Collect a forwarded/pasted post during audit."""
    posts = context.user_data.get("audit_posts", [])
    text = update.message.text or update.message.caption or ""
    text = text.strip()

    if not text:
        await update.message.reply_text("⚠️ I can only analyze text posts. Forward another one.")
        return AUDIT_COLLECTING

    posts.append(text)
    context.user_data["audit_posts"] = posts
    count = len(posts)

    if count >= 10:
        return await _run_audit(update, context)

    await update.message.reply_text(
        f"✅ Post {count} collected. ({count}/10)\n\n"
        "Forward another, or send /done to run the audit."
    )
    return AUDIT_COLLECTING


async def audit_done(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/done — Trigger the audit with collected posts."""
    posts = context.user_data.get("audit_posts", [])
    if len(posts) < 3:
        await update.message.reply_text(f"⚠️ Need at least 3 posts. You have {len(posts)}. Forward more.")
        return AUDIT_COLLECTING
    return await _run_audit(update, context)


async def _run_audit(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Execute the AI audit on collected posts."""
    uid = update.effective_user.id
    posts = context.user_data.pop("audit_posts", [])
    numbered = "\n\n".join(f"--- Post {i+1} ---\n{p}" for i, p in enumerate(posts))

    profile = await _T(get_user_profile, uid)

    # ── Try enqueue to worker queue ───────────────────────────────────────
    if redis_client.queue_available():
        import json as _json

        # Send placeholder
        placeholder = await update.message.reply_text("⚡ Analyzing your channel...")

        audit_prompt = prompt_registry.get_with_vars("audit", posts=numbered)
        
        job = _json.dumps({
            "chat_id": placeholder.chat_id,
            "message_id": placeholder.message_id,
            "user_id": uid,
            "command": "audit",
            "system_prompt": audit_prompt,
            "user_prompt": "Perform the audit.",
            "max_tokens": 900,
            "profile": profile,
            "cache_ttl": 0,
            "has_rewrite": False,
            "delivery": "send_long",
            "keyboard": "none",
            "audit_meta": {
                "posts_analyzed": len(posts),
            },
        })

        enqueued = await redis_client.enqueue_job(job)
        if enqueued:
            logger.info("Audit job enqueued: user=%s posts=%d", uid, len(posts))
            return -1  # Worker will deliver the result

        # Enqueue failed - fall through to inline execution
        logger.warning("Audit enqueue failed for user %s - falling back to inline.", uid)
        try:
            await placeholder.delete()
        except Exception:
            pass

    # Queue unavailable
    await update.message.reply_text(
        "⚠️ The AI system is temporarily unavailable. Please try again in a moment."
    )

    return -1  # ConversationHandler.END


# ─── /boost — Paste post → score + improve ───────────────────────────────────
# Uses ConversationHandler. State BOOST_WAITING = 3

BOOST_WAITING = 3


async def cmd_boost(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """/boost — Start the performance analysis flow."""
    if not await _ensure_user(update):
        await _db_error_reply(update)
        return -1
    await update.message.reply_text(
        "🚀 Post Performance Analyzer\n\n"
        "Paste the post you want to analyze and improve:"
    )
    return BOOST_WAITING


async def boost_receive_text(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """User pasted a post — analyze it."""
    if not await _enforce_limits(update):
        return -1

    uid = update.effective_user.id
    original = update.message.text.strip()
    if not original or len(original) < 10:
        await update.message.reply_text("⚠️ Paste a real post (at least 10 characters).")
        return BOOST_WAITING

    profile = await _T(get_user_profile, uid)

    # ── Try enqueue to worker queue ───────────────────────────────────────
    if redis_client.queue_available():
        import json as _json

        # Send placeholder
        placeholder = await update.message.reply_text("⚡ Analyzing your post...")

        analyze_prompt = prompt_registry.get_with_vars("boost_analyze", original_post=original)
        
        job = _json.dumps({
            "chat_id": placeholder.chat_id,
            "message_id": placeholder.message_id,
            "user_id": uid,
            "command": "boost_analyze",
            "system_prompt": analyze_prompt,
            "user_prompt": "Analyze this post.",
            "max_tokens": 600,
            "profile": profile,
            "cache_ttl": 0,
            "has_rewrite": False,
            "delivery": "edit",
            "keyboard": "boost_action",
            "session_writes": [
                {"key": "boost_original", "value": original},
                {"key": "boost_analysis", "value": "$RESULT"},
            ],
        })

        enqueued = await redis_client.enqueue_job(job)
        if enqueued:
            logger.info("Boost analyze job enqueued: user=%s", uid)
            return -1  # Worker will deliver the result

        # Enqueue failed - fall through to inline execution
        logger.warning("Boost analyze enqueue failed for user %s - falling back to inline.", uid)
        try:
            await placeholder.delete()
        except Exception:
            pass

    # Queue unavailable
    await update.message.reply_text(
        "⚠️ The AI system is temporarily unavailable. Please try again in a moment."
    )

    return -1  # ConversationHandler.END


async def _handle_boost_action(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    """Handle boost:apply / boost:cancel callbacks."""
    query = update.callback_query
    action = data.split(":")[1]

    if action == "cancel":
        return await query.edit_message_text("❌ Boost cancelled.")

    uid = query.from_user.id

    # Retrieve stored original + analysis (Redis first, fall back to context.user_data)
    original = (await redis_client.get_session(uid, "boost_original")
                or context.user_data.get("boost_original", ""))
    analysis = (await redis_client.get_session(uid, "boost_analysis")
                or context.user_data.get("boost_analysis", ""))

    if not original or not analysis:
        return await query.edit_message_text("⚠️ Session expired. Use /boost again.")

    profile = await _T(get_user_profile, uid)

    # ── Try enqueue to worker queue ───────────────────────────────────────
    if redis_client.queue_available():
        import json as _json

        # Edit message to placeholder
        try:
            await query.edit_message_text("⚡ Improving your post...")
        except Exception:
            pass

        rewrite_prompt = prompt_registry.get_with_vars(
            "boost_rewrite", original_post=original, analysis=analysis,
        )

        job = _json.dumps({
            "chat_id": query.message.chat_id,
            "message_id": query.message.message_id,
            "user_id": uid,
            "command": "boost_rewrite",
            "system_prompt": rewrite_prompt,
            "user_prompt": "Apply the improvements.",
            "max_tokens": 500,
            "profile": profile,
            "cache_ttl": 0,
            "has_rewrite": True,
            "delivery": "edit",
            "keyboard": "rewrite",
            "session_writes": [
                {"key": "last_ai_response", "value": "$RESULT"},
            ],
        })

        enqueued = await redis_client.enqueue_job(job)
        if enqueued:
            logger.info("Boost rewrite job enqueued: user=%s", uid)
            return  # Worker will deliver the result

        # Enqueue failed - fall through to inline execution
        logger.warning("Boost rewrite enqueue failed for user %s - falling back to inline.", uid)

    # Queue unavailable
    await query.edit_message_text(
        "⚠️ The AI system is temporarily unavailable. Please try again in a moment."
    )


# ─── /autopilot — Configure daily auto-generation (premium only) ─────────────

async def cmd_autopilot(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """/autopilot — Set up or manage autopilot content delivery."""
    if not await _ensure_user(update):
        return await _db_error_reply(update)
    uid = update.effective_user.id

    if not await _T(is_user_premium, uid):
        return await update.message.reply_text(
            "🤖 Autopilot is a Pro feature.\n\n"
            "It generates and sends content to you on a daily schedule.\n"
            "Use /upgrade to unlock it. 🚀"
        )

    settings = await _T(get_autopilot_settings, uid)

    if settings and settings.get("active"):
        await update.message.reply_text(
            "🤖 Autopilot is ACTIVE\n"
            "━━━━━━━━━━━━━━━━━━━━━━━\n\n"
            f"Posts/day: {settings['posts_per_day']}\n"
            f"Content mix: {settings['content_mix']}\n"
            f"Last delivery: {settings.get('last_delivered_at') or 'never'}\n\n"
            "To change settings, tap below.\n"
            "To stop: /autopilot stop",
            reply_markup=autopilot_frequency_keyboard(),
        )
    else:
        await update.message.reply_text(
            "🤖 Autopilot Content Engine\n\n"
            "I'll generate and send you fresh content daily.\n"
            "First, how many posts per day?",
            reply_markup=autopilot_frequency_keyboard(),
        )


async def cmd_autopilot_stop(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle '/autopilot stop' to disable autopilot."""
    uid = update.effective_user.id
    args = _extract_topic(context)
    if args.lower() == "stop":
        await _T(save_autopilot_settings, uid, active=False)
        await update.message.reply_text("🛑 Autopilot stopped. Use /autopilot to restart anytime.")
        return
    # If not "stop", delegate to the main handler
    await cmd_autopilot(update, context)


async def _handle_autopilot_step(update: Update, context: ContextTypes.DEFAULT_TYPE, data: str) -> None:
    """Handle autopilot configuration callbacks."""
    query = update.callback_query
    uid = query.from_user.id
    parts = data.split(":")

    if parts[1] == "freq":
        ppd = int(parts[2])
        context.user_data["autopilot_ppd"] = ppd
        await query.edit_message_text(
            f"✅ {ppd} post(s)/day\n\nContent mix?",
            reply_markup=autopilot_mix_keyboard(),
        )

    elif parts[1] == "mix":
        mix = parts[2]
        ppd = context.user_data.pop("autopilot_ppd", 1)

        # Get niche from profile
        profile = await _T(get_user_profile, uid)
        niche = (profile.get("niche") if profile else "") or ""
        tone = (profile.get("tone") if profile else "") or ""

        await _T(
            save_autopilot_settings, uid,
            posts_per_day=ppd, niche_override=niche,
            tone_override=tone, content_mix=mix, active=True,
        )

        await query.edit_message_text(
            "✅ Autopilot activated!\n\n"
            f"Posts/day: {ppd}\n"
            f"Mix: {mix}\n"
            f"Niche: {niche or '(from profile)'}\n\n"
            "You'll receive fresh content daily at 09:00 UTC. 🤖\n"
            "Stop anytime: /autopilot stop"
        )
        logger.info("User %s activated autopilot: %d ppd, mix=%s", uid, ppd, mix)


# ─── AUTOPILOT DELIVERY (called by scheduler in main.py) ────────────────────

async def send_autopilot_content(bot) -> None:
    """Generate and deliver content to all active autopilot users."""
    try:
        configs = await _T(get_active_autopilot_users)
    except Exception as exc:
        logger.error("Autopilot: failed to get configs: %s", exc)
        return

    import datetime as dt
    now = dt.datetime.now(dt.timezone.utc)

    for cfg in configs:
        tid = cfg["telegram_id"]
        ppd = cfg.get("posts_per_day", 1)
        mix = cfg.get("content_mix", "mixed")
        last = cfg.get("last_delivered_at")

        # Skip if delivered less than (24/ppd) hours ago
        if last:
            hours_since = (now - last).total_seconds() / 3600
            interval = 24.0 / ppd
            if hours_since < interval:
                continue

        try:
            profile = await _T(get_user_profile, tid)
            niche = cfg.get("niche_override") or (profile.get("niche") if profile else "") or "general"

            # Choose prompt based on content mix
            if mix == "viral":
                sys_prompt, user_prompt = VIRAL_SYSTEM, f"Viral post about trending {niche} topic."
            elif mix == "ideas":
                sys_prompt, user_prompt = DAILY_IDEAS_SYSTEM, f"3 content ideas for {niche}."
            elif mix == "posts":
                sys_prompt, user_prompt = POST_SYSTEM, f"Post about a trending {niche} topic."
            else:
                # Mixed: rotate
                import random as _rng
                choice = _rng.choice(["post", "viral", "engage"])
                prompts_map = {"post": POST_SYSTEM, "viral": VIRAL_SYSTEM, "engage": ENGAGE_SYSTEM}
                sys_prompt = prompts_map[choice]
                user_prompt = f"Create a {choice} about a trending {niche} topic."

            result = await ask_openai(sys_prompt, user_prompt, max_tokens=500, profile=profile)
            await bot.send_message(chat_id=tid, text=_with_branding(result.text))
            await _safe_record(tid, "autopilot")
            await _record_gen(tid, "autopilot", result)
            await _T(update_autopilot_delivery, tid)
            logger.info("Autopilot delivered to user %s", tid)

        except Exception as exc:
            logger.warning("Autopilot failed for user %s: %s", tid, exc)

        await asyncio.sleep(0.5)
