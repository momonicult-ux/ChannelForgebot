# ChannelForgeBot — SaaS Edition

AI-powered Telegram post generator, refactored into a production multi-service architecture.

## Project Structure

```
channelforgebot/
├── main.py                    # Entry point: Telegram polling + HTTP server
├── config.py                  # Centralised env-var config with validation
├── schema.sql                 # PostgreSQL schema (auto-applied on startup)
├── requirements.txt
├── Procfile                   # Railway / Render process definition
├── Dockerfile
├── .env.example               # Template for environment variables
│
├── bot/
│   ├── handlers.py            # Handler registration
│   ├── commands.py            # All /command + callback query handlers
│   ├── openai_service.py      # OpenAI client, prompts, profile injection
│   └── keyboards.py           # Inline keyboard builders
│
├── database/
│   ├── db.py                  # Connection pool + all SQL helpers
│   └── models.py              # Table documentation dataclasses
│
├── billing/
│   └── stripe_webhooks.py     # POST /webhook/stripe handler
│
└── analytics/
    └── metrics.py             # Structured log events
```

## Commands

### Original (preserved)
| Command | Description |
|---------|-------------|
| `/start` | Welcome message with tier info |
| `/help` | Full command reference with live quota |
| `/post <topic>` | Full channel post: hook → body → CTA → hashtags |
| `/meme <topic>` | Meme-style caption with punchline |
| `/caption <topic>` | Short punchy standalone caption |
| `/ideas` | 5 trending post ideas |
| `/upgrade` | Pro plan pricing card |
| `/stats` | Owner-only analytics dashboard |

### New Creator Commands
| Command | Description |
|---------|-------------|
| `/hook <topic>` | 10 viral opening hooks |
| `/trend <niche>` | 5 trending discussion topics |
| `/weekpack` | Full weekly content pack (7 ideas + 3 posts + 5 captions) |
| `/profile` | Configure channel niche, audience, tone, style |

### Inline Features
- **Rewrite workflow**: After every post/meme/caption, inline buttons appear: Shorter · More viral · Add CTA · Another version
- **Channel mode**: When used in groups/channels, shows Publish · Rewrite · Cancel preview buttons
- **Profile setup**: Guided inline-button flow for configuring your channel profile

## Setup

### 1. Environment Variables

Copy `.env.example` to `.env` and fill in your values:

```bash
cp .env.example .env
```

Required:
- `TELEGRAM_BOT_TOKEN` — from [@BotFather](https://t.me/BotFather)
- `OPENAI_API_KEY` — from [OpenAI](https://platform.openai.com)
- `DATABASE_URL` — PostgreSQL connection string

Optional:
- `OWNER_TELEGRAM_ID` — your Telegram user ID (enables /stats)
- `STRIPE_SECRET_KEY` + `STRIPE_WEBHOOK_SECRET` — for billing
- `DAILY_LIMIT`, `COOLDOWN_SECONDS`, `OPENAI_MODEL`, `HTTP_PORT`

### 2. Database

The bot auto-applies `schema.sql` on startup using `CREATE TABLE IF NOT EXISTS`, so no manual migration step is needed. Just provide a valid PostgreSQL `DATABASE_URL`.

### 3. Install & Run

```bash
pip install -r requirements.txt
python main.py
```

The bot starts two services concurrently:
1. **Telegram polling** — handles all bot commands
2. **HTTP server** on port 8080 — serves `/health` and `/webhook/stripe`

## Deployment

### Railway

1. Connect your repo to Railway
2. Add a PostgreSQL plugin
3. Set environment variables in the Railway dashboard
4. Railway auto-detects the `Procfile`

### Render

1. Create a new Web Service pointing at your repo
2. Set **Build Command**: `pip install -r requirements.txt`
3. Set **Start Command**: `python main.py`
4. Add a PostgreSQL database from Render's dashboard
5. Set environment variables in the Render dashboard

### Docker

```bash
docker build -t channelforgebot .
docker run --env-file .env -p 8080:8080 channelforgebot
```

## Stripe Integration

### Webhook Setup

1. In your Stripe dashboard, create a webhook endpoint pointing at:
   ```
   https://your-domain.com/webhook/stripe
   ```
2. Subscribe to events: `checkout.session.completed`, `customer.subscription.deleted`, `invoice.payment_failed`
3. Copy the webhook signing secret to `STRIPE_WEBHOOK_SECRET`

### Checkout Flow

When creating a Stripe Checkout Session, include the Telegram user ID in metadata:

```python
session = stripe.checkout.Session.create(
    mode="subscription",
    line_items=[{"price": "price_xxx", "quantity": 1}],
    metadata={"telegram_id": str(user_telegram_id)},
    success_url="https://channelforgebot.com/success",
    cancel_url="https://channelforgebot.com/cancel",
)
```

The webhook handler reads `metadata.telegram_id` to link the payment to the correct bot user.

## Architecture Notes

- **Rate limiting** is database-backed via `usage_events` — counts today's rows per user. No more JSON files.
- **Premium status** is derived from the `users.plan` column, updated by Stripe webhooks or manual SQL.
- **User profiles** are injected into every OpenAI system prompt, personalising all generated content.
- **Cooldown** remains in-memory (intentional — it's ephemeral by nature and doesn't need persistence).
- **Growth footer** on every AI response: `⚡ Want posts like this? → @ChannelForgeBot`
- **Health endpoint** at `GET /health` returns `{"status": "ok"}` for uptime monitoring.
- **Schema auto-migration** — `schema.sql` is applied on every startup with `IF NOT EXISTS` guards.
