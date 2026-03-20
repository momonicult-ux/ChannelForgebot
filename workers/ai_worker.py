"""
workers/ai_worker.py — Background AI job processor.

v10: Upgraded with 5 new capabilities:
  1. Keyboard resolver — string identifier → InlineKeyboardMarkup
  2. send_long delivery — chunked messages for long output
  3. Generic session_writes — list of {key, value} with $RESULT substitution
  4. Audit post-processing — score extraction + DB write
  5. Multi-call execution — parallel ask_openai() with labeled assembly

Backward compatible: v9 payloads (with has_rewrite) still work.
The worker loop (ai_worker_loop) is unchanged.

Architecture:
  Handler → LPUSH job to Redis → reply "⚡ Generating..."
  Worker  → BRPOP job → generate → post-process → deliver
"""

import asyncio
import json
import logging
import re
import time

from telegram import Bot
from telegram.error import BadRequest as TelegramBadRequest

import redis_client
from bot.keyboards import (
    boost_action_keyboard,
    regenerate_keyboard,
    rewrite_keyboard,
    viral_cta_keyboard,
)
from bot.openai_service import ask_openai, OpenAIResult
from config import (
    AI_WORKER_CONCURRENCY,
    AI_WORKER_TIMEOUT,
    BOT_USERNAME,
    BRAND_FOOTER,
    MAX_MSG_CHARS,
    OPENAI_MODEL,
    TRUNCATION_NOTE,
)
from database.db import record_generation_event, record_usage_event, save_channel_audit

logger = logging.getLogger(__name__)

_semaphore: asyncio.Semaphore | None = None

_jobs_processed: int = 0
_jobs_failed: int = 0


def get_worker_stats() -> dict[str, int]:
    """Return worker metrics for /stats dashboard."""
    return {
        "jobs_processed": _jobs_processed,
        "jobs_failed": _jobs_failed,
    }


# ─── BRANDING ────────────────────────────────────────────────────────────────

def _with_branding(text: str) -> str:
    combined = text + BRAND_FOOTER
    if len(combined) <= MAX_MSG_CHARS:
        return combined
    budget = MAX_MSG_CHARS - len(BRAND_FOOTER) - len(TRUNCATION_NOTE)
    return text[:budget].rstrip() + TRUNCATION_NOTE + BRAND_FOOTER


# ─── KEYBOARD RESOLVER ───────────────────────────────────────────────────────
# Maps a string identifier from the job payload to an InlineKeyboardMarkup.
# Uses real keyboard builders from bot/keyboards.py (no circular import risk).

def _build_rewrite_markup():
    """Rewrite buttons + viral CTA row — the most common keyboard."""
    from telegram import InlineKeyboardMarkup
    rows = rewrite_keyboard().inline_keyboard
    cta_row = viral_cta_keyboard(BOT_USERNAME).inline_keyboard[0]
    return InlineKeyboardMarkup(rows + [cta_row])


def _resolve_keyboard(name: str | None):
    """
    Resolve a keyboard identifier to an InlineKeyboardMarkup or None.

    Supported values:
      "rewrite"           → 6 rewrite buttons + CTA
      "cta_only"          → "Create Your Own Post" URL button
      "regenerate:hook"   → "Generate new batch" for hooks
      "regenerate:trend"  → "Generate new batch" for trends
      "boost_action"      → "Apply improvements" / "Cancel"
      "none" / None       → No keyboard
    """
    if not name or name == "none":
        return None
    if name == "rewrite":
        return _build_rewrite_markup()
    if name == "cta_only":
        return viral_cta_keyboard(BOT_USERNAME)
    if name.startswith("regenerate:"):
        cmd = name.split(":", 1)[1]
        return regenerate_keyboard(cmd)
    if name == "boost_action":
        return boost_action_keyboard()
    logger.warning("Worker: unknown keyboard '%s', using none.", name)
    return None


# ─── RECORDING ───────────────────────────────────────────────────────────────

async def _safe_record(tid: int, cmd: str) -> None:
    try:
        await asyncio.to_thread(record_usage_event, tid, cmd)
    except Exception as exc:
        logger.warning("Worker: usage record failed: %s", exc)


async def _record_gen(
    tid: int, cmd: str, result: OpenAIResult | None = None,
    *, error_type: str | None = None, latency_ms: int = 0,
) -> None:
    try:
        if result:
            await asyncio.to_thread(
                record_generation_event, tid, command=cmd, model=result.model,
                prompt_tokens=result.prompt_tokens, completion_tokens=result.completion_tokens,
                total_tokens=result.total_tokens, estimated_cost_usd=result.estimated_cost_usd,
                latency_ms=result.latency_ms, status="success",
            )
        else:
            await asyncio.to_thread(
                record_generation_event, tid, command=cmd, model=OPENAI_MODEL,
                prompt_tokens=0, completion_tokens=0, total_tokens=0,
                estimated_cost_usd=0.0, latency_ms=latency_ms,
                status="error", error_type=error_type,
            )
    except Exception as exc:
        logger.warning("Worker: gen record failed: %s", exc)


# ─── DELIVERY MODES ─────────────────────────────────────────────────────────

async def _deliver_edit(bot: Bot, chat_id: int, message_id: int, text: str, markup) -> None:
    """Edit the placeholder message in-place.  Falls back to send if deleted."""
    try:
        await bot.edit_message_text(
            chat_id=chat_id, message_id=message_id,
            text=text, reply_markup=markup,
        )
    except TelegramBadRequest as exc:
        if "message to edit not found" in str(exc).lower():
            await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
        elif "not modified" not in str(exc).lower():
            logger.warning("Worker: edit failed chat=%s: %s", chat_id, exc)


async def _deliver_send_long(bot: Bot, chat_id: int, message_id: int, text: str, markup) -> None:
    """
    Delete the placeholder, then send result as chunked messages.

    Splits at paragraph boundaries, respects 4096-char Telegram limit.
    Keyboard is attached only to the final chunk.
    """
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

    limit = 4096
    if len(text) <= limit:
        await bot.send_message(chat_id=chat_id, text=text, reply_markup=markup)
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
        await bot.send_message(
            chat_id=chat_id, text=chunk,
            reply_markup=markup if is_last else None,
        )


async def _deliver_error(bot: Bot, chat_id: int, message_id: int, delivery: str, text: str) -> None:
    """Deliver an error message using the appropriate mode."""
    try:
        if delivery == "send_long":
            try:
                await bot.delete_message(chat_id=chat_id, message_id=message_id)
            except Exception:
                pass
            await bot.send_message(chat_id=chat_id, text=text)
        else:
            await bot.edit_message_text(chat_id=chat_id, message_id=message_id, text=text)
    except Exception:
        pass


# ─── GENERATION MODES ────────────────────────────────────────────────────────

async def _execute_single_call(job: dict) -> OpenAIResult:
    """Standard single ask_openai() call with semaphore + timeout."""
    async with _semaphore:
        return await asyncio.wait_for(
            ask_openai(
                job["system_prompt"], job["user_prompt"],
                max_tokens=min(job.get("max_tokens", 450), 800),
                profile=job.get("profile"),
                cache_ttl=job.get("cache_ttl", 0),
            ),
            timeout=AI_WORKER_TIMEOUT,
        )


async def _execute_multi_call(job: dict) -> tuple[str, OpenAIResult | None]:
    """
    Execute multiple ask_openai() calls in parallel, assemble labeled output.

    Each call acquires the semaphore independently so they're still bounded
    by AI_WORKER_CONCURRENCY globally.  Partial failures produce
    "(generation failed)" under the label instead of aborting everything.

    Returns (assembled_text, best_result) where best_result is the first
    successful OpenAIResult (used for generation metrics).
    """
    calls = job["calls"]
    profile = job.get("profile")
    cache_ttl = job.get("cache_ttl", 0)

    async def _run_one(call_spec: dict) -> OpenAIResult:
        async with _semaphore:
            return await asyncio.wait_for(
                ask_openai(
                    call_spec["system_prompt"],
                    call_spec["user_prompt"],
                    max_tokens=min(call_spec.get("max_tokens", 450), 800),
                    profile=profile,
                    cache_ttl=cache_ttl,
                ),
                timeout=AI_WORKER_TIMEOUT,
            )

    results = await asyncio.gather(
        *[_run_one(c) for c in calls],
        return_exceptions=True,
    )

    separator = "\n\n━━━━━━━━━━━━━━━━━━━━━━━\n\n"
    parts: list[str] = []
    best_result: OpenAIResult | None = None

    for call_spec, result in zip(calls, results):
        label = call_spec.get("label", "Result")
        if isinstance(result, Exception):
            parts.append(f"{label}\n(generation failed)")
            logger.warning("Worker: multi-call '%s' failed: %s", label, result)
        else:
            parts.append(f"{label}\n{result.text}")
            if best_result is None:
                best_result = result

    return separator.join(parts), best_result


# ─── POST-PROCESSING ────────────────────────────────────────────────────────

async def _write_sessions(uid: int, session_writes: list[dict], result_text: str) -> None:
    """
    Write session values to Redis after successful generation.

    Each entry: {"key": "some_key", "value": "$RESULT"} or {"key": "x", "value": "literal"}
    The special token "$RESULT" is replaced with the generated text.
    """
    for entry in session_writes:
        key = entry.get("key", "")
        value = entry.get("value", "")
        if not key:
            continue
        if value == "$RESULT":
            value = result_text
        await redis_client.set_session(uid, key, value)


async def _process_audit(uid: int, audit_meta: dict, result_text: str) -> None:
    """Extract score from audit output and save to channel_audits table."""
    posts_analyzed = audit_meta.get("posts_analyzed", 0)
    score_match = re.search(r"Overall Score:\s*(\d+)/10", result_text)
    score = int(score_match.group(1)) if score_match else 0
    try:
        await asyncio.to_thread(save_channel_audit, uid, posts_analyzed, score, result_text)
    except Exception as exc:
        logger.warning("Worker: audit DB save failed: %s", exc)


# ─── JOB DISPATCH ────────────────────────────────────────────────────────────

async def _process_job(bot: Bot, raw_payload: str) -> None:
    """
    Process a single AI generation job.

    Three phases:
      1. GENERATE — single call or multi-call parallel
      2. POST-PROCESS — record, session writes, audit DB write
      3. DELIVER — edit placeholder or send chunked long-message

    Backward compatible with v9 payloads: if "delivery" and "keyboard"
    are missing, they're derived from "has_rewrite".
    """
    global _jobs_processed, _jobs_failed

    try:
        job = json.loads(raw_payload)
    except json.JSONDecodeError as exc:
        logger.error("Worker: invalid job payload: %s", exc)
        _jobs_failed += 1
        return

    chat_id = job["chat_id"]
    message_id = job["message_id"]
    uid = job["user_id"]
    command = job["command"]

    # ── v9 backward compatibility ────────────────────────────────────────
    if "delivery" not in job:
        job["delivery"] = "edit"
    if "keyboard" not in job:
        has_rewrite = job.get("has_rewrite", True)
        job["keyboard"] = "rewrite" if has_rewrite else "cta_only"
    if "session_writes" not in job:
        has_rewrite = job.get("has_rewrite", True)
        if has_rewrite:
            job["session_writes"] = [{"key": "last_ai_response", "value": "$RESULT"}]
        else:
            job["session_writes"] = []

    delivery = job["delivery"]
    t0 = time.monotonic()

    try:
        # ── Phase 1: GENERATE ────────────────────────────────────────────
        if job.get("calls"):
            result_text, best_result = await _execute_multi_call(job)
        else:
            best_result = await _execute_single_call(job)
            result_text = best_result.text

        # ── Phase 2: POST-PROCESS ────────────────────────────────────────
        await _safe_record(uid, command)
        if best_result:
            await _record_gen(uid, command, best_result)

        session_writes = job.get("session_writes", [])
        if session_writes:
            await _write_sessions(uid, session_writes, result_text)

        audit_meta = job.get("audit_meta")
        if audit_meta:
            await _process_audit(uid, audit_meta, result_text)

        # ── Phase 3: DELIVER ─────────────────────────────────────────────
        branded = _with_branding(result_text)
        markup = _resolve_keyboard(job.get("keyboard"))

        if delivery == "send_long":
            await _deliver_send_long(bot, chat_id, message_id, branded, markup)
        else:
            await _deliver_edit(bot, chat_id, message_id, branded, markup)

        _jobs_processed += 1
        tokens = best_result.total_tokens if best_result else 0
        latency = best_result.latency_ms if best_result else 0
        logger.info(
            "Worker: OK user=%s cmd=%s delivery=%s tokens=%d latency=%dms",
            uid, command, delivery, tokens, latency,
        )

    except asyncio.TimeoutError:
        _jobs_failed += 1
        latency_ms = int((time.monotonic() - t0) * 1000)
        await _record_gen(uid, command, error_type="WorkerTimeout", latency_ms=latency_ms)
        await _deliver_error(bot, chat_id, message_id, delivery,
                             "⏱️ Generation took too long. Please try again.")
        logger.warning("Worker: timeout user=%s cmd=%s", uid, command)

    except Exception as exc:
        _jobs_failed += 1
        latency_ms = int((time.monotonic() - t0) * 1000)
        await _record_gen(uid, command, error_type=type(exc).__name__, latency_ms=latency_ms)
        await _deliver_error(bot, chat_id, message_id, delivery,
                             f"❌ Error generating {command}. Please try again.")
        logger.error("Worker: failed user=%s cmd=%s error=%s", uid, command, exc)


# ─── WORKER LOOP (unchanged from v9) ────────────────────────────────────────

async def ai_worker_loop(bot: Bot) -> None:
    """
    Main worker loop.  Runs forever, consuming jobs from the Redis queue.

    Jobs are processed sequentially. The Semaphore inside _process_job
    controls how many OpenAI calls run in parallel across all workers.
    """
    global _semaphore
    
    # Initialize semaphore only once (first worker to reach this line)
    if _semaphore is None:
        _semaphore = asyncio.Semaphore(AI_WORKER_CONCURRENCY)
        logger.info(
            "Initialized shared semaphore (concurrency=%d).",
            AI_WORKER_CONCURRENCY,
        )

    logger.info(
        "AI worker started (concurrency=%d, timeout=%ds).",
        AI_WORKER_CONCURRENCY, AI_WORKER_TIMEOUT,
    )

    while True:
        try:
            raw = await redis_client.dequeue_job(timeout=2)
            if raw is None:
                continue

            await _process_job(bot, raw)

        except asyncio.CancelledError:
            logger.info("AI worker shutting down.")
            break
        except Exception as exc:
            logger.error("AI worker loop error: %s", exc)
            await asyncio.sleep(1)
