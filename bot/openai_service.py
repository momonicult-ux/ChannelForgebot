"""
bot/openai_service.py — OpenAI client, retry logic, cost estimation.

v7: All prompt text moved to prompts/*.txt files.
    Backward-compatible aliases (POST_SYSTEM, MEME_SYSTEM etc.) are
    thin wrappers around prompts.get() so existing imports still work.
"""

import asyncio
import logging
import random
import time
from dataclasses import dataclass

import openai
from openai import AsyncOpenAI

import prompts
import redis_client
from config import (
    AI_CACHE_TTL, OPENAI_API_KEY, OPENAI_INPUT_COST_PER_TOKEN, OPENAI_MODEL,
    OPENAI_OUTPUT_COST_PER_TOKEN, OPENAI_TIMEOUT_SECONDS,
)

logger = logging.getLogger(__name__)
client = AsyncOpenAI(api_key=OPENAI_API_KEY)


@dataclass(frozen=True, slots=True)
class OpenAIResult:
    text: str
    prompt_tokens: int
    completion_tokens: int
    total_tokens: int
    model: str
    latency_ms: int
    estimated_cost_usd: float


# ─── BACKWARD-COMPAT ALIASES ────────────────────────────────────────────────
# Existing code imports these by name. They now resolve from prompts/*.txt.

POST_SYSTEM = prompts.get("post")
MEME_SYSTEM = prompts.get("meme")
CAPTION_SYSTEM = prompts.get("caption")
IDEAS_SYSTEM = prompts.get("ideas")
HOOK_SYSTEM = prompts.get("hook")
HOOK_REGEN_SYSTEM = prompts.get("hook_regen")
TREND_SYSTEM = prompts.get("trend")
WEEKPACK_SYSTEM = prompts.get("weekpack")
VIRAL_SYSTEM = prompts.get("viral")
DAILY_IDEAS_SYSTEM = prompts.get("daily_ideas")
ENGAGE_SYSTEM = prompts.get("engage")

REWRITE_SHORTER_SYSTEM = prompts.get("rewrite.shorter")
REWRITE_VIRAL_SYSTEM = prompts.get("rewrite.viral")
REWRITE_CTA_SYSTEM = prompts.get("rewrite.cta")
REWRITE_ANOTHER_SYSTEM = prompts.get("rewrite.another")
REWRITE_EMOTIONAL_SYSTEM = prompts.get("rewrite.emotional")
REWRITE_SMARTER_SYSTEM = prompts.get("rewrite.smarter")
REWRITE_MEME_SYSTEM = prompts.get("rewrite.meme")


# ─── INPUT SANITISATION ─────────────────────────────────────────────────────

_INJECTION_MARKERS = (
    "ignore all", "ignore previous", "ignore above", "disregard",
    "forget your instructions", "new instructions", "system prompt",
    "you are now", "act as", "pretend you", "override", "jailbreak",
)

def _sanitize_user_input(text: str) -> str:
    lowered = text.lower()
    for marker in _INJECTION_MARKERS:
        if marker in lowered:
            start = lowered.index(marker)
            text = text[:start] + "[filtered]" + text[start + len(marker):]
            lowered = text.lower()
    return text.strip()


# ─── PROFILE INJECTION ──────────────────────────────────────────────────────

def _inject_profile(system_prompt: str, profile: dict[str, str] | None) -> str:
    if not profile:
        return system_prompt
    parts = []
    if profile.get("niche"):
        parts.append(f"Channel niche: {profile['niche']}")
    if profile.get("audience"):
        parts.append(f"Target audience: {profile['audience']}")
    if profile.get("tone"):
        parts.append(f"Preferred tone: {profile['tone']}")
    if profile.get("post_style"):
        parts.append(f"Post style: {profile['post_style']}")
    if not parts:
        return system_prompt
    return system_prompt + (
        "\n\n---\nUser's channel profile:\n" + "\n".join(f"• {p}" for p in parts)
    )


# ─── CORE API CALL ──────────────────────────────────────────────────────────

_RETRYABLE = (openai.APIConnectionError, openai.InternalServerError, openai.RateLimitError)
_MAX_RETRIES = 3

def _estimate_cost(prompt_tokens: int, completion_tokens: int) -> float:
    return prompt_tokens * OPENAI_INPUT_COST_PER_TOKEN + completion_tokens * OPENAI_OUTPUT_COST_PER_TOKEN


async def ask_openai(
    system_prompt: str, user_prompt: str,
    max_tokens: int = 450, profile: dict[str, str] | None = None,
    cache_ttl: int = 0,
) -> OpenAIResult:
    """
    Send a chat-completion request to OpenAI, with optional Redis caching.

    Args:
        system_prompt: The system message (role instructions).
        user_prompt: The user message (topic/content).
        max_tokens: Max output tokens.
        profile: User's channel profile (injected into system prompt).
        cache_ttl: Seconds to cache the response in Redis.
                   0 = no caching (default — user-specific content).
                   >0 = cache for this many seconds.

    Returns:
        OpenAIResult with text, token counts, cost, latency, and cache flag.
    """
    full_system = _inject_profile(system_prompt, profile)
    clean_prompt = _sanitize_user_input(user_prompt)

    # ── Cache check (before any API call) ────────────────────────────────
    cache_key = None
    if cache_ttl > 0:
        # Build profile string for cache key (empty string if no profile)
        profile_str = "|".join(f"{k}={v}" for k, v in sorted((profile or {}).items()) if v)
        cache_key = redis_client.make_cache_key(full_system, clean_prompt, profile_str)
        cached = await redis_client.cache_get(cache_key)
        if cached is not None:
            logger.info("AI cache HIT — returning cached response (key=%s)", cache_key[-8:])
            return OpenAIResult(
                text=cached["text"],
                prompt_tokens=cached.get("prompt_tokens", 0),
                completion_tokens=cached.get("completion_tokens", 0),
                total_tokens=cached.get("total_tokens", 0),
                model=cached.get("model", OPENAI_MODEL),
                latency_ms=0,  # No API call made
                estimated_cost_usd=0.0,  # No cost incurred
            )

    # ── API call with retry ──────────────────────────────────────────────
    last_exc: Exception | None = None
    t_start = time.monotonic()

    for attempt in range(_MAX_RETRIES):
        try:
            response = await client.chat.completions.create(
                model=OPENAI_MODEL,
                messages=[
                    {"role": "system", "content": full_system},
                    {"role": "user", "content": clean_prompt},
                ],
                max_tokens=max_tokens, temperature=0.85,
                timeout=OPENAI_TIMEOUT_SECONDS,
            )
            if not response.choices:
                raise RuntimeError("OpenAI returned empty response.")

            latency_ms = int((time.monotonic() - t_start) * 1000)
            usage = response.usage
            pt = usage.prompt_tokens if usage else 0
            ct = usage.completion_tokens if usage else 0
            tt = usage.total_tokens if usage else 0
            model = response.model or OPENAI_MODEL
            cost = _estimate_cost(pt, ct)
            text = response.choices[0].message.content.strip()

            logger.info("OpenAI OK: model=%s tokens=%d cost=$%.5f latency=%dms", model, tt, cost, latency_ms)

            result = OpenAIResult(
                text=text,
                prompt_tokens=pt, completion_tokens=ct, total_tokens=tt,
                model=model, latency_ms=latency_ms, estimated_cost_usd=cost,
            )

            # ── Cache store (after successful API call) ──────────────────
            if cache_key and cache_ttl > 0:
                await redis_client.cache_set(
                    cache_key,
                    {
                        "text": text,
                        "prompt_tokens": pt,
                        "completion_tokens": ct,
                        "total_tokens": tt,
                        "model": model,
                    },
                    ttl=cache_ttl,
                )

            return result

        except _RETRYABLE as exc:
            last_exc = exc
            if attempt < _MAX_RETRIES - 1:
                delay = (1.0 * (2 ** attempt)) + random.uniform(0, 0.5)
                logger.warning("OpenAI transient (%d/%d): %s — retry in %.1fs",
                               attempt + 1, _MAX_RETRIES, type(exc).__name__, delay)
                await asyncio.sleep(delay)
            else:
                logger.error("OpenAI failed (%d/%d): %s", attempt + 1, _MAX_RETRIES, exc)

    raise last_exc  # type: ignore[misc]
