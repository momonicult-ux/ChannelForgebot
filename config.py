"""
config.py — Centralised configuration for ChannelForgeBot SaaS.
v6: Added REFERRAL_BONUS_REQUESTS, BOT_USERNAME.
"""

import os
import sys

from dotenv import load_dotenv

load_dotenv()

# ─── Required ────────────────────────────────────────────────────────────────
TELEGRAM_BOT_TOKEN: str = os.getenv("TELEGRAM_BOT_TOKEN", "")
OPENAI_API_KEY: str = os.getenv("OPENAI_API_KEY", "")
DATABASE_URL: str = os.getenv("DATABASE_URL", "")

_missing: list[str] = []
if not TELEGRAM_BOT_TOKEN:
    _missing.append("TELEGRAM_BOT_TOKEN")
if not OPENAI_API_KEY:
    _missing.append("OPENAI_API_KEY")
if not DATABASE_URL:
    _missing.append("DATABASE_URL")
if _missing:
    print(f"FATAL: Missing env vars: {', '.join(_missing)}", file=sys.stderr)
    raise EnvironmentError(f"Missing env vars: {', '.join(_missing)}")

# ─── Optional ────────────────────────────────────────────────────────────────
OWNER_ID: int = int(os.getenv("OWNER_TELEGRAM_ID", "0"))
STRIPE_SECRET_KEY: str = os.getenv("STRIPE_SECRET_KEY", "")
STRIPE_WEBHOOK_SECRET: str = os.getenv("STRIPE_WEBHOOK_SECRET", "")
REDIS_URL: str = os.getenv("REDIS_URL", "")
BOT_USERNAME: str = os.getenv("BOT_USERNAME", "ChannelForgeBot")

# ─── Rate limits ─────────────────────────────────────────────────────────────
DAILY_LIMIT: int = int(os.getenv("DAILY_LIMIT", "5"))
PREMIUM_DAILY_LIMIT: int = int(os.getenv("PREMIUM_DAILY_LIMIT", "200"))
COOLDOWN_SECONDS: float = float(os.getenv("COOLDOWN_SECONDS", "2"))
REFERRAL_BONUS_REQUESTS: int = int(os.getenv("REFERRAL_BONUS_REQUESTS", "10"))

# ─── Tuning ──────────────────────────────────────────────────────────────────
MAX_TOPIC_CHARS: int = 200
MAX_MSG_CHARS: int = 4000
OPENAI_TIMEOUT_SECONDS: int = 20
OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
OPENAI_INPUT_COST_PER_TOKEN: float = float(os.getenv("OPENAI_INPUT_COST", "0.00000015"))
OPENAI_OUTPUT_COST_PER_TOKEN: float = float(os.getenv("OPENAI_OUTPUT_COST", "0.0000006"))

# ─── AI cache ────────────────────────────────────────────────────────────────
AI_CACHE_TTL: int = int(os.getenv("AI_CACHE_TTL", "86400"))  # 24 hours default

# ─── AI workers ──────────────────────────────────────────────────────────────
AI_WORKER_CONCURRENCY: int = int(os.getenv("AI_WORKER_CONCURRENCY", "5"))
AI_WORKER_TIMEOUT: int = int(os.getenv("AI_WORKER_TIMEOUT", "120"))  # max seconds per job

# ─── Branding ────────────────────────────────────────────────────────────────
BRAND_FOOTER: str = "\n\n⚡ Generated with @ChannelForgeBot"
TRUNCATION_NOTE: str = "\n\n…[trimmed]"

# ─── HTTP ────────────────────────────────────────────────────────────────────
HTTP_HOST: str = os.getenv("HTTP_HOST", "0.0.0.0")
HTTP_PORT: int = int(os.getenv("PORT", os.getenv("HTTP_PORT", "8080")))
