"""
database/models.py — Table documentation and schema path.

The actual table creation is handled by schema.sql, applied at startup via
database.db.init_db().  This module documents the table shapes for IDE
autocompletion and developer reference.
"""

from dataclasses import dataclass
from datetime import datetime


@dataclass
class User:
    id: int
    telegram_id: int
    username: str | None
    first_name: str | None
    plan: str  # 'free' | 'pro'
    stripe_customer_id: str | None
    created_at: datetime


@dataclass
class Subscription:
    id: int
    user_id: int
    stripe_subscription_id: str
    status: str  # 'active' | 'canceled' | 'past_due'
    current_period_end: datetime | None
    created_at: datetime
    updated_at: datetime


@dataclass
class UsageEvent:
    id: int
    user_id: int
    command: str
    created_at: datetime


@dataclass
class UserProfile:
    id: int
    user_id: int
    niche: str
    audience: str
    tone: str
    post_style: str
    updated_at: datetime
