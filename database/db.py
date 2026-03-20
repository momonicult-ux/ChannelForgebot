"""
database/db.py — PostgreSQL helpers.
v6: Added referral system + get_premium_telegram_ids for daily scheduler.
"""

import logging
from contextlib import contextmanager
from datetime import datetime
from decimal import Decimal
from pathlib import Path
from typing import Any, Generator

import psycopg2
import psycopg2.extras
from psycopg2.pool import ThreadedConnectionPool

from config import DATABASE_URL, DAILY_LIMIT, REFERRAL_BONUS_REQUESTS

logger = logging.getLogger(__name__)
_pool: ThreadedConnectionPool | None = None


def init_db() -> None:
    global _pool
    try:
        _pool = ThreadedConnectionPool(minconn=2, maxconn=10, dsn=DATABASE_URL)
        logger.info("DB pool created.")
    except psycopg2.OperationalError as exc:
        logger.critical("Cannot connect to DB: %s", exc)
        raise SystemExit(f"DB connection failed: {exc}") from exc
    _apply_schema()


def close_db() -> None:
    global _pool
    if _pool:
        _pool.closeall()
        logger.info("DB pool closed.")
        _pool = None


@contextmanager
def _get_conn() -> Generator:
    if _pool is None:
        raise RuntimeError("DB not initialised.")
    conn = _pool.getconn()
    try:
        if conn.closed:
            _pool.putconn(conn, close=True)
            conn = _pool.getconn()
        yield conn
        conn.commit()
    except Exception:
        conn.rollback()
        raise
    finally:
        _pool.putconn(conn)


def _apply_schema() -> None:
    schema_path = Path(__file__).resolve().parent.parent / "schema.sql"
    try:
        sql = schema_path.read_text(encoding="utf-8")
        with _get_conn() as conn:
            with conn.cursor() as cur:
                cur.execute(sql)
        logger.info("Schema applied from %s.", schema_path)
    except FileNotFoundError:
        logger.error("schema.sql not found at %s", schema_path)
    except psycopg2.Error as exc:
        logger.error("Schema failed: %s", exc)
        raise


# ─── USER CRUD ───────────────────────────────────────────────────────────────

def create_user_if_not_exists(
    telegram_id: int,
    username: str | None = None,
    first_name: str | None = None,
) -> dict[str, Any]:
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """INSERT INTO users (telegram_id, username, first_name)
                VALUES (%s, %s, %s)
                ON CONFLICT (telegram_id) DO UPDATE
                    SET username   = COALESCE(EXCLUDED.username,   users.username),
                        first_name = COALESCE(EXCLUDED.first_name, users.first_name)
                RETURNING id, telegram_id, plan, created_at;""",
                (telegram_id, username, first_name),
            )
            return dict(cur.fetchone())


def get_user_by_telegram_id(telegram_id: int) -> dict[str, Any] | None:
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT id, telegram_id, plan, stripe_customer_id, created_at "
                "FROM users WHERE telegram_id = %s;", (telegram_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_telegram_id_by_user_id(user_id: int) -> int | None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT telegram_id FROM users WHERE id = %s;", (user_id,))
            row = cur.fetchone()
            return row[0] if row else None


def is_new_user(telegram_id: int) -> bool:
    """True if the user was just created (no profile yet)."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT 1 FROM user_profiles WHERE user_id = "
                "(SELECT id FROM users WHERE telegram_id = %s);",
                (telegram_id,),
            )
            return cur.fetchone() is None


def set_user_plan(telegram_id: int, plan: str) -> None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("UPDATE users SET plan = %s WHERE telegram_id = %s;", (plan, telegram_id))


def set_stripe_customer_id(telegram_id: int, stripe_customer_id: str) -> None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE users SET stripe_customer_id = %s WHERE telegram_id = %s;",
                (stripe_customer_id, telegram_id),
            )


# ─── PREMIUM ─────────────────────────────────────────────────────────────────

def is_user_premium(telegram_id: int) -> bool:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT plan FROM users WHERE telegram_id = %s;", (telegram_id,))
            row = cur.fetchone()
            return row is not None and row[0] == "pro"


def get_premium_telegram_ids() -> list[int]:
    """Return all premium user Telegram IDs — used by daily ideas scheduler."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT telegram_id FROM users WHERE plan = 'pro';")
            return [row[0] for row in cur.fetchall()]


def upsert_subscription(
    user_id: int, stripe_subscription_id: str, status: str,
    current_period_end: datetime | None = None,
) -> None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO subscriptions
                    (user_id, stripe_subscription_id, status, current_period_end, updated_at)
                VALUES (%s, %s, %s, %s, NOW())
                ON CONFLICT (stripe_subscription_id) DO UPDATE
                    SET status=EXCLUDED.status, current_period_end=EXCLUDED.current_period_end, updated_at=NOW();""",
                (user_id, stripe_subscription_id, status, current_period_end),
            )


def get_user_id_by_stripe_customer(stripe_customer_id: str) -> int | None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT id FROM users WHERE stripe_customer_id = %s;", (stripe_customer_id,))
            row = cur.fetchone()
            return row[0] if row else None


# ─── STRIPE IDEMPOTENCY ─────────────────────────────────────────────────────

def is_stripe_event_processed(event_id: str) -> bool:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1 FROM stripe_events WHERE event_id = %s;", (event_id,))
            return cur.fetchone() is not None


def record_stripe_event(event_id: str, event_type: str) -> None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO stripe_events (event_id, event_type) VALUES (%s, %s) ON CONFLICT DO NOTHING;",
                (event_id, event_type),
            )


# ─── REFERRALS (v6) ─────────────────────────────────────────────────────────

def record_referral(referrer_telegram_id: int, referred_telegram_id: int) -> bool:
    """
    Record a referral. Returns True if newly created, False if already exists.
    Also sets referred_by on the new user's row.
    """
    with _get_conn() as conn:
        with conn.cursor() as cur:
            # Get internal IDs
            cur.execute("SELECT id FROM users WHERE telegram_id = %s;", (referrer_telegram_id,))
            referrer_row = cur.fetchone()
            cur.execute("SELECT id FROM users WHERE telegram_id = %s;", (referred_telegram_id,))
            referred_row = cur.fetchone()
            if not referrer_row or not referred_row:
                return False
            referrer_id = referrer_row[0]
            referred_id = referred_row[0]
            if referrer_id == referred_id:
                return False  # Can't refer yourself

            cur.execute(
                "INSERT INTO referrals (referrer_id, referred_user_id) VALUES (%s, %s) "
                "ON CONFLICT (referred_user_id) DO NOTHING RETURNING id;",
                (referrer_id, referred_id),
            )
            created = cur.fetchone() is not None
            if created:
                cur.execute(
                    "UPDATE users SET referred_by = %s WHERE id = %s;",
                    (referrer_id, referred_id),
                )
            return created


def get_referral_count(telegram_id: int) -> int:
    """Count how many users this person has referred."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM referrals WHERE referrer_id = "
                "(SELECT id FROM users WHERE telegram_id = %s);",
                (telegram_id,),
            )
            return cur.fetchone()[0]


def get_daily_limit_for_user(telegram_id: int) -> int:
    """
    Return the effective daily limit including referral bonuses.
    Each referral adds REFERRAL_BONUS_REQUESTS to the base DAILY_LIMIT.
    """
    bonus = get_referral_count(telegram_id) * REFERRAL_BONUS_REQUESTS
    return DAILY_LIMIT + bonus


def get_referral_link(telegram_id: int, bot_username: str) -> str:
    """Build the user's referral deep-link."""
    return f"https://t.me/{bot_username}?start=ref_{telegram_id}"


# ─── USAGE TRACKING ──────────────────────────────────────────────────────────

def record_usage_event(telegram_id: int, command: str) -> None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO usage_events (user_id, command) "
                "SELECT id, %s FROM users WHERE telegram_id = %s;",
                (command, telegram_id),
            )


def get_usage_today(telegram_id: int) -> int:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT COUNT(*) FROM usage_events "
                "WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s) "
                "AND created_at >= CURRENT_DATE;",
                (telegram_id,),
            )
            return cur.fetchone()[0]


def get_remaining_daily_quota(telegram_id: int) -> int | str:
    if is_user_premium(telegram_id):
        return "∞"
    used = get_usage_today(telegram_id)
    limit = get_daily_limit_for_user(telegram_id)
    return max(0, limit - used)


def check_rate_limit(telegram_id: int) -> bool:
    if is_user_premium(telegram_id):
        return True
    used = get_usage_today(telegram_id)
    limit = get_daily_limit_for_user(telegram_id)
    return used < limit


# ─── GENERATION TRACKING ────────────────────────────────────────────────────

def record_generation_event(
    telegram_id: int, *, command: str, model: str,
    prompt_tokens: int, completion_tokens: int, total_tokens: int,
    estimated_cost_usd: float, latency_ms: int,
    status: str = "success", error_type: str | None = None,
) -> None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO generation_events
                    (user_id, command, model, prompt_tokens, completion_tokens,
                     total_tokens, estimated_cost_usd, latency_ms, status, error_type)
                SELECT id, %s, %s, %s, %s, %s, %s, %s, %s, %s
                FROM users WHERE telegram_id = %s;""",
                (command, model, prompt_tokens, completion_tokens,
                 total_tokens, estimated_cost_usd, latency_ms,
                 status, error_type, telegram_id),
            )


def get_generation_stats() -> dict[str, Any]:
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    (SELECT COUNT(*) FROM generation_events WHERE created_at >= CURRENT_DATE) AS today_generations,
                    (SELECT COALESCE(SUM(total_tokens), 0) FROM generation_events WHERE created_at >= CURRENT_DATE) AS today_tokens,
                    (SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM generation_events WHERE created_at >= CURRENT_DATE) AS today_cost,
                    (SELECT COUNT(*) FROM generation_events) AS total_generations,
                    (SELECT COALESCE(SUM(total_tokens), 0) FROM generation_events) AS total_tokens,
                    (SELECT COALESCE(SUM(estimated_cost_usd), 0) FROM generation_events) AS total_cost,
                    (SELECT COALESCE(AVG(latency_ms), 0) FROM generation_events WHERE created_at >= CURRENT_DATE AND status = 'success') AS avg_latency_ms;
            """)
            row = dict(cur.fetchone())
            for k in ("today_cost", "total_cost", "avg_latency_ms"):
                if isinstance(row[k], Decimal):
                    row[k] = float(row[k])
            return row


# ─── USER PROFILES ───────────────────────────────────────────────────────────

def save_user_profile(
    telegram_id: int, *, niche: str = "", audience: str = "",
    tone: str = "", post_style: str = "",
) -> None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO user_profiles (user_id, niche, audience, tone, post_style, updated_at)
                SELECT id, %s, %s, %s, %s, NOW() FROM users WHERE telegram_id = %s
                ON CONFLICT (user_id) DO UPDATE
                    SET niche=EXCLUDED.niche, audience=EXCLUDED.audience,
                        tone=EXCLUDED.tone, post_style=EXCLUDED.post_style, updated_at=NOW();""",
                (niche, audience, tone, post_style, telegram_id),
            )


def get_user_profile(telegram_id: int) -> dict[str, str] | None:
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT niche, audience, tone, post_style FROM user_profiles "
                "WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s);",
                (telegram_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


# ─── ANALYTICS ───────────────────────────────────────────────────────────────

def get_stats() -> dict[str, Any]:
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("""
                SELECT
                    (SELECT COUNT(*) FROM users) AS total_users,
                    (SELECT COUNT(*) FROM users WHERE plan = 'pro') AS premium_users,
                    (SELECT COUNT(*) FROM usage_events) AS total_requests,
                    (SELECT COUNT(*) FROM usage_events WHERE created_at >= CURRENT_DATE) AS today_requests,
                    (SELECT COUNT(DISTINCT user_id) FROM usage_events WHERE created_at >= CURRENT_DATE) AS today_active_users,
                    (SELECT COUNT(*) FROM referrals) AS total_referrals;
            """)
            return dict(cur.fetchone())


def get_command_breakdown() -> dict[str, int]:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT command, COUNT(*) FROM usage_events GROUP BY command ORDER BY COUNT(*) DESC;"
            )
            return {row[0]: row[1] for row in cur.fetchall()}


# ─── CHANNEL AUDITS (v7) ────────────────────────────────────────────────────

def save_channel_audit(telegram_id: int, posts_analyzed: int, overall_score: int, audit_text: str) -> None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO channel_audits (user_id, posts_analyzed, overall_score, audit_text)
                SELECT id, %s, %s, %s FROM users WHERE telegram_id = %s;""",
                (posts_analyzed, overall_score, audit_text, telegram_id),
            )


# ─── AUTOPILOT SETTINGS (v7) ────────────────────────────────────────────────

def save_autopilot_settings(
    telegram_id: int, *, posts_per_day: int = 1,
    niche_override: str = "", tone_override: str = "",
    content_mix: str = "mixed", active: bool = True,
) -> None:
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """INSERT INTO autopilot_settings
                    (user_id, posts_per_day, niche_override, tone_override, content_mix, active)
                SELECT id, %s, %s, %s, %s, %s FROM users WHERE telegram_id = %s
                ON CONFLICT (user_id) DO UPDATE
                    SET posts_per_day=EXCLUDED.posts_per_day,
                        niche_override=EXCLUDED.niche_override,
                        tone_override=EXCLUDED.tone_override,
                        content_mix=EXCLUDED.content_mix,
                        active=EXCLUDED.active;""",
                (posts_per_day, niche_override, tone_override, content_mix, active, telegram_id),
            )


def get_autopilot_settings(telegram_id: int) -> dict[str, Any] | None:
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                "SELECT posts_per_day, niche_override, tone_override, content_mix, active, last_delivered_at "
                "FROM autopilot_settings WHERE user_id = "
                "(SELECT id FROM users WHERE telegram_id = %s);",
                (telegram_id,),
            )
            row = cur.fetchone()
            return dict(row) if row else None


def get_active_autopilot_users() -> list[dict[str, Any]]:
    """Return all active autopilot configs with telegram_id for the scheduler."""
    with _get_conn() as conn:
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                """SELECT u.telegram_id, a.posts_per_day, a.niche_override,
                          a.tone_override, a.content_mix, a.last_delivered_at
                FROM autopilot_settings a
                JOIN users u ON u.id = a.user_id
                WHERE a.active = TRUE AND u.plan = 'pro';"""
            )
            return [dict(row) for row in cur.fetchall()]


def update_autopilot_delivery(telegram_id: int) -> None:
    """Stamp last_delivered_at after successful delivery."""
    with _get_conn() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "UPDATE autopilot_settings SET last_delivered_at = NOW() "
                "WHERE user_id = (SELECT id FROM users WHERE telegram_id = %s);",
                (telegram_id,),
            )
