"""
redis_client.py — Redis connection, rate limiting, AI caching, and session storage.

v8: Added AI response caching (cache_get / cache_set).
    Fixed rate limiter to check-then-increment (no longer increments rejected requests).
"""

import hashlib
import json
import logging
from datetime import datetime, timezone

import redis.asyncio as aioredis

from config import AI_CACHE_TTL, COOLDOWN_SECONDS, DAILY_LIMIT, PREMIUM_DAILY_LIMIT, REDIS_URL

logger = logging.getLogger(__name__)

_pool: aioredis.Redis | None = None

_SESSION_TTL = 3600  # 1 hour


async def init_redis() -> None:
    global _pool
    if not REDIS_URL:
        logger.info("REDIS_URL not set — Redis disabled, using DB/memory fallback.")
        return
    try:
        _pool = aioredis.from_url(
            REDIS_URL, decode_responses=True,
            socket_connect_timeout=3, socket_timeout=2,
        )
        await _pool.ping()
        logger.info("Redis connected.")
    except Exception as exc:
        logger.warning("Redis connection failed (%s) — using fallback.", exc)
        _pool = None


async def close_redis() -> None:
    global _pool
    if _pool:
        await _pool.aclose()
        logger.info("Redis closed.")
        _pool = None


def is_available() -> bool:
    return _pool is not None


# ─── Rate limiting ───────────────────────────────────────────────────────────

def _quota_key(tid: int) -> str:
    return f"cfb:quota:{tid}:{datetime.now(timezone.utc).strftime('%Y-%m-%d')}"


async def check_rate_limit(tid: int, is_premium: bool) -> tuple[bool, int]:
    """
    Atomic check-and-increment rate limiter.

    Uses a Lua script so that rejected requests do NOT inflate the counter.
    Returns (allowed, remaining).  remaining=-1 means Redis unavailable.
    """
    if _pool is None:
        return True, -1
    limit = PREMIUM_DAILY_LIMIT if is_premium else DAILY_LIMIT
    key = _quota_key(tid)
    # Lua: read current count, reject if >= limit, otherwise INCR and set TTL
    _LUA_CHECK_AND_INCR = """
    local current = tonumber(redis.call('GET', KEYS[1]) or '0')
    if current >= tonumber(ARGV[1]) then
        return {0, 0}
    end
    local new_count = redis.call('INCR', KEYS[1])
    if new_count == 1 then
        redis.call('EXPIRE', KEYS[1], 90000)
    end
    return {1, tonumber(ARGV[1]) - new_count}
    """
    try:
        result = await _pool.eval(_LUA_CHECK_AND_INCR, 1, key, str(limit))
        allowed = bool(result[0])
        remaining = max(0, int(result[1]))
        return allowed, remaining
    except Exception as exc:
        logger.warning("Redis rate-limit error: %s", exc)
        return True, -1


async def check_cooldown(tid: int) -> float:
    if _pool is None:
        return -1.0
    try:
        was_set = await _pool.set(
            f"cfb:cd:{tid}", "1", nx=True, px=int(COOLDOWN_SECONDS * 1000),
        )
        if was_set:
            return 0.0
        ttl_ms = await _pool.pttl(f"cfb:cd:{tid}")
        return ttl_ms / 1000.0 if ttl_ms > 0 else 0.0
    except Exception as exc:
        logger.warning("Redis cooldown error: %s", exc)
        return -1.0


async def get_remaining_quota(tid: int, is_premium: bool) -> int | str:
    if _pool is None:
        return "?"
    limit = PREMIUM_DAILY_LIMIT if is_premium else DAILY_LIMIT
    try:
        count = await _pool.get(_quota_key(tid))
        used = int(count) if count else 0
        rem = max(0, limit - used)
        return "∞" if is_premium and rem > 50 else rem
    except Exception:
        return "?"


# ─── Session storage (v6) ───────────────────────────────────────────────────
# Stores the last generated text per user for the rewrite engine.
# Falls back to context.user_data if Redis is unavailable.

async def set_session(tid: int, key: str, value: str) -> bool:
    """Store a session value. Returns True on success."""
    if _pool is None:
        return False
    try:
        await _pool.set(f"cfb:sess:{tid}:{key}", value, ex=_SESSION_TTL)
        return True
    except Exception:
        return False


async def get_session(tid: int, key: str) -> str | None:
    """Retrieve a session value. Returns None if not found or Redis down."""
    if _pool is None:
        return None
    try:
        return await _pool.get(f"cfb:sess:{tid}:{key}")
    except Exception:
        return None


# ─── AI Response Cache (v8) ──────────────────────────────────────────────────
#
# Caches OpenAI responses keyed by a SHA-256 hash of the full prompt content.
# This means identical prompts (same system prompt + user prompt + profile)
# return cached results without calling OpenAI again.
#
# Key format:  cfb:ai:{sha256_hex[:16]}
# Value:       JSON-serialized dict with all OpenAIResult fields
# TTL:         Configurable per call, defaults to AI_CACHE_TTL (24h)
#
# NOT cached:  Rewrites (user-specific text), audits (forwarded posts),
#              boost analysis (user-pasted content).  Callers control this
#              by passing cache_ttl=0 to ask_openai().

def make_cache_key(system_prompt: str, user_prompt: str, profile_str: str) -> str:
    """
    Build a deterministic cache key from the full prompt inputs.

    The key includes the profile so two users with different niches
    don't share cached results.  Truncated to 16 hex chars (64 bits) —
    collision probability is negligible under 1M daily unique prompts.
    """
    raw = f"{system_prompt}|{user_prompt}|{profile_str}"
    digest = hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]
    return f"cfb:ai:{digest}"


async def cache_get(cache_key: str) -> dict | None:
    """
    Retrieve a cached AI response.

    Returns the deserialized dict (OpenAIResult fields) or None if
    not found / Redis unavailable.
    """
    if _pool is None:
        return None
    try:
        raw = await _pool.get(cache_key)
        if raw is None:
            return None
        data = json.loads(raw)
        logger.debug("AI cache HIT: %s", cache_key)
        return data
    except Exception as exc:
        logger.warning("AI cache read error: %s", exc)
        return None


async def cache_set(cache_key: str, data: dict, ttl: int | None = None) -> bool:
    """
    Store an AI response in cache.

    Args:
        cache_key: From make_cache_key().
        data: Dict with OpenAIResult fields (text, tokens, model, etc.).
        ttl: Seconds until expiry.  Defaults to AI_CACHE_TTL.

    Returns True on success.
    """
    if _pool is None:
        return False
    if ttl is None:
        ttl = AI_CACHE_TTL
    if ttl <= 0:
        return False  # Caller explicitly disabled caching
    try:
        await _pool.set(cache_key, json.dumps(data), ex=ttl)
        logger.debug("AI cache SET: %s (ttl=%ds)", cache_key, ttl)
        return True
    except Exception as exc:
        logger.warning("AI cache write error: %s", exc)
        return False


async def get_cache_stats() -> dict[str, int]:
    """
    Return basic cache stats for /stats dashboard.
    Counts keys matching the cfb:ai:* pattern.
    """
    if _pool is None:
        return {"cached_responses": 0}
    try:
        cursor, keys = await _pool.scan(match="cfb:ai:*", count=1000)
        count = len(keys)
        while cursor:
            cursor, batch = await _pool.scan(cursor=cursor, match="cfb:ai:*", count=1000)
            count += len(batch)
        return {"cached_responses": count}
    except Exception:
        return {"cached_responses": 0}


# ─── AI Job Queue (v9) ──────────────────────────────────────────────────────
#
# In-process async workers consume jobs from a Redis list.
# Handlers LPUSH job payloads; workers BRPOP them.
#
# Key:   cfb:jobs:ai  — Redis LIST of JSON job payloads
#
# Why not ARQ/Celery?  The worker must call bot.edit_message_text() to
# deliver results.  That requires the bot object, which lives in this
# process.  A separate worker process would need its own bot instance
# (doubling Telegram API connections) and a result-delivery callback
# mechanism.  In-process workers avoid all of that.

_QUEUE_KEY = "cfb:jobs:ai"
MAX_QUEUE_SIZE = 200


def queue_available() -> bool:
    """True if the job queue can accept jobs (Redis connected)."""
    return _pool is not None


async def enqueue_job(payload: str) -> bool:
    """
    Push a JSON job payload onto the AI work queue.
    Returns True on success, False if Redis unavailable or queue full.
    """
    if _pool is None:
        return False
    try:
        queue_depth = await _pool.llen(_QUEUE_KEY)
        if queue_depth >= MAX_QUEUE_SIZE:
            logger.warning("Queue full — rejecting job")
            return False

        try:
            job_data = json.loads(payload)
        except json.JSONDecodeError as exc:
            logger.error("Invalid JSON payload: %s", exc)
            return False

        job_data["job_id"] = job_data.get("job_id") or __import__("uuid").uuid4().hex
        job_data["created_at"] = job_data.get("created_at") or __import__("time").time()

        await _pool.lpush(_QUEUE_KEY, json.dumps(job_data))
        logger.info("Queue job accepted: %s", job_data["job_id"])
        return True
    except Exception as exc:
        logger.warning("Queue enqueue error: %s", exc)
        return False


async def dequeue_job(timeout: int = 2) -> str | None:
    """
    Pop a job from the queue, blocking up to `timeout` seconds.
    Returns the JSON payload string, or None if no job / Redis down.
    """
    if _pool is None:
        return None
    try:
        result = await _pool.brpop(_QUEUE_KEY, timeout=timeout)
        if result is None:
            return None
        # brpop returns (key, value) tuple
        return result[1]
    except Exception as exc:
        # Connection errors during BRPOP are common on reconnect
        if "timeout" not in str(exc).lower():
            logger.warning("Queue dequeue error: %s", exc)
        return None


async def get_queue_depth() -> int:
    """Return current number of pending jobs."""
    if _pool is None:
        return 0
    try:
        return await _pool.llen(_QUEUE_KEY)
    except Exception:
        return 0
