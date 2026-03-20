-- ChannelForgeBot SaaS — PostgreSQL Schema v7

CREATE TABLE IF NOT EXISTS users (
    id              SERIAL PRIMARY KEY,
    telegram_id     BIGINT UNIQUE NOT NULL,
    username        TEXT,
    first_name      TEXT,
    plan            TEXT NOT NULL DEFAULT 'free',
    stripe_customer_id TEXT,
    referred_by     INTEGER REFERENCES users(id),
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS subscriptions (
    id                      SERIAL PRIMARY KEY,
    user_id                 INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    stripe_subscription_id  TEXT UNIQUE NOT NULL,
    status                  TEXT NOT NULL DEFAULT 'active',
    current_period_end      TIMESTAMPTZ,
    created_at              TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    updated_at              TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS usage_events (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    command     TEXT NOT NULL,
    created_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS user_profiles (
    id          SERIAL PRIMARY KEY,
    user_id     INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    niche       TEXT DEFAULT '',
    audience    TEXT DEFAULT '',
    tone        TEXT DEFAULT '',
    post_style  TEXT DEFAULT '',
    updated_at  TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS stripe_events (
    event_id     TEXT PRIMARY KEY,
    event_type   TEXT NOT NULL,
    processed_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS generation_events (
    id                SERIAL PRIMARY KEY,
    user_id           INTEGER REFERENCES users(id) ON DELETE SET NULL,
    command           TEXT NOT NULL,
    model             TEXT NOT NULL,
    prompt_tokens     INTEGER NOT NULL DEFAULT 0,
    completion_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens      INTEGER NOT NULL DEFAULT 0,
    estimated_cost_usd NUMERIC(10, 6) NOT NULL DEFAULT 0,
    latency_ms        INTEGER NOT NULL DEFAULT 0,
    status            TEXT NOT NULL DEFAULT 'success',
    error_type        TEXT,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

CREATE TABLE IF NOT EXISTS referrals (
    id              SERIAL PRIMARY KEY,
    referrer_id     INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    referred_user_id INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW(),
    UNIQUE (referred_user_id)
);

-- v7: Channel audit history
CREATE TABLE IF NOT EXISTS channel_audits (
    id              SERIAL PRIMARY KEY,
    user_id         INTEGER NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    posts_analyzed  INTEGER NOT NULL DEFAULT 0,
    overall_score   INTEGER,
    audit_text      TEXT NOT NULL,
    created_at      TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- v7: Autopilot configuration
CREATE TABLE IF NOT EXISTS autopilot_settings (
    id                SERIAL PRIMARY KEY,
    user_id           INTEGER UNIQUE NOT NULL REFERENCES users(id) ON DELETE CASCADE,
    niche_override    TEXT DEFAULT '',
    posts_per_day     INTEGER NOT NULL DEFAULT 1,
    tone_override     TEXT DEFAULT '',
    content_mix       TEXT NOT NULL DEFAULT 'mixed',
    active            BOOLEAN NOT NULL DEFAULT TRUE,
    last_delivered_at TIMESTAMPTZ,
    created_at        TIMESTAMPTZ NOT NULL DEFAULT NOW()
);

-- Indexes
CREATE INDEX IF NOT EXISTS idx_users_telegram_id ON users(telegram_id);
CREATE INDEX IF NOT EXISTS idx_usage_events_user_created ON usage_events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_subscriptions_user_id ON subscriptions(user_id);
CREATE INDEX IF NOT EXISTS idx_subscriptions_stripe_id ON subscriptions(stripe_subscription_id);
CREATE INDEX IF NOT EXISTS idx_generation_events_user_created ON generation_events(user_id, created_at);
CREATE INDEX IF NOT EXISTS idx_generation_events_created ON generation_events(created_at);
CREATE INDEX IF NOT EXISTS idx_stripe_events_type ON stripe_events(event_type);
CREATE INDEX IF NOT EXISTS idx_referrals_referrer ON referrals(referrer_id);
CREATE INDEX IF NOT EXISTS idx_channel_audits_user ON channel_audits(user_id);
CREATE INDEX IF NOT EXISTS idx_autopilot_active ON autopilot_settings(active) WHERE active = TRUE;
