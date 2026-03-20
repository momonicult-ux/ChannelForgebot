"""
main.py — ChannelForgeBot SaaS entry point.
v6: Added daily content ideas scheduler for premium users.
"""

import asyncio
import logging
import signal
import sys

from aiohttp import web
from telegram import Update
from telegram.ext import Application

from config import HTTP_HOST, HTTP_PORT, OWNER_ID, TELEGRAM_BOT_TOKEN
from database.db import close_db, init_db
from redis_client import init_redis, close_redis
from bot.handlers import register_handlers
from bot.commands import send_daily_ideas, send_autopilot_content
from workers.ai_worker import ai_worker_loop
from billing.stripe_webhooks import stripe_webhook

_log_handlers: list[logging.Handler] = [logging.StreamHandler()]
try:
    _log_handlers.append(logging.FileHandler("analytics.log", encoding="utf-8"))
except OSError:
    pass

logging.basicConfig(
    format="%(asctime)s | %(levelname)s | %(name)s — %(message)s",
    level=logging.INFO, handlers=_log_handlers,
)
logger = logging.getLogger(__name__)


async def health_handler(request: web.Request) -> web.Response:
    return web.json_response({"status": "ok"})


def _build_http_app() -> web.Application:
    app = web.Application()
    app.router.add_get("/health", health_handler)
    app.router.add_post("/webhook/stripe", stripe_webhook)
    return app


# ─── DAILY IDEAS SCHEDULER (Feature 3) ──────────────────────────────────────

async def _daily_ideas_loop(tg_app: Application) -> None:
    """
    Background task that sends daily content ideas to premium users.

    Runs every 24 hours.  On first boot, waits until next 09:00 UTC,
    then repeats every 86400 seconds.
    """
    import datetime as dt

    while True:
        # Calculate seconds until next 09:00 UTC
        now = dt.datetime.now(dt.timezone.utc)
        target = now.replace(hour=9, minute=0, second=0, microsecond=0)
        if now >= target:
            target += dt.timedelta(days=1)
        wait_seconds = (target - now).total_seconds()

        logger.info("Daily ideas: next run in %.0f seconds (at %s UTC)", wait_seconds, target.isoformat())
        await asyncio.sleep(wait_seconds)

        try:
            logger.info("Daily ideas: starting delivery to premium users.")
            await send_daily_ideas(tg_app.bot)
            logger.info("Daily ideas: delivery complete.")
        except Exception as exc:
            logger.error("Daily ideas: delivery failed: %s", exc)


async def _autopilot_loop(tg_app: Application) -> None:
    """
    Background task that delivers autopilot content.
    Runs every hour, checks which users are due for delivery.
    """
    while True:
        await asyncio.sleep(3600)  # Check every hour
        try:
            logger.info("Autopilot: checking for pending deliveries.")
            await send_autopilot_content(tg_app.bot)
        except Exception as exc:
            logger.error("Autopilot loop error: %s", exc)


async def run() -> None:
    try:
        init_db()
        logger.info("Database initialised.")
    except SystemExit:
        raise
    except Exception as exc:
        logger.critical("DB init error: %s", exc)
        raise SystemExit(1) from exc

    await init_redis()

    tg_app = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    register_handlers(tg_app)
    await tg_app.initialize()
    await tg_app.start()
    await tg_app.updater.start_polling(allowed_updates=Update.ALL_TYPES)
    logger.info("Bot started (owner=%s).", OWNER_ID)

    http_app = _build_http_app()
    runner = web.AppRunner(http_app)
    await runner.setup()
    site = web.TCPSite(runner, HTTP_HOST, HTTP_PORT)
    await site.start()
    logger.info("HTTP on %s:%s", HTTP_HOST, HTTP_PORT)

    # Start background tasks
    ideas_task = asyncio.create_task(_daily_ideas_loop(tg_app))
    autopilot_task = asyncio.create_task(_autopilot_loop(tg_app))
    
    # Start 3 AI worker tasks for improved throughput and fault tolerance
    worker_tasks = [
        asyncio.create_task(ai_worker_loop(tg_app.bot))
        for _ in range(3)
    ]
    logger.info("Started %d AI workers.", len(worker_tasks))

    stop_event = asyncio.Event()

    def _signal_handler() -> None:
        logger.info("Shutdown signal.")
        stop_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, _signal_handler)
        except NotImplementedError:
            signal.signal(sig, lambda s, f: _signal_handler())

    await stop_event.wait()

    logger.info("Shutting down…")
    
    # Cancel all background tasks
    ideas_task.cancel()
    autopilot_task.cancel()
    for task in worker_tasks:
        task.cancel()
    
    # Wait for all tasks to actually exit before closing resources
    # Bounded timeout prevents indefinite hangs if workers are stuck
    try:
        await asyncio.wait_for(
            asyncio.gather(
                ideas_task,
                autopilot_task,
                *worker_tasks,
                return_exceptions=True,
            ),
            timeout=30.0,
        )
        logger.info("All background tasks exited.")
    except asyncio.TimeoutError:
        logger.warning("Shutdown timeout after 30s - forcing exit (some tasks may not have exited cleanly).")
    
    # Now safe to close resources (all tasks guaranteed stopped or timed out)
    for label, coro in [
        ("polling", tg_app.updater.stop()),
        ("bot stop", tg_app.stop()),
        ("bot shutdown", tg_app.shutdown()),
        ("HTTP cleanup", runner.cleanup()),
    ]:
        try:
            await coro
        except Exception as exc:
            logger.warning("Teardown %s: %s", label, exc)

    await close_redis()
    close_db()
    logger.info("Shutdown complete.")


def main() -> None:
    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        sys.exit(0)
    except SystemExit as exc:
        sys.exit(exc.code)


if __name__ == "__main__":
    main()
