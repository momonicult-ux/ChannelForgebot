"""
billing/stripe_webhooks.py — Stripe webhook handler for subscription lifecycle.

v5: Added idempotency protection via stripe_events table.
    Duplicate Stripe events (retries, replays) are detected and skipped
    before any business logic runs.
"""

import logging

from aiohttp import web

from config import STRIPE_SECRET_KEY, STRIPE_WEBHOOK_SECRET
from database.db import (
    get_telegram_id_by_user_id,
    get_user_by_telegram_id,
    get_user_id_by_stripe_customer,
    is_stripe_event_processed,
    record_stripe_event,
    set_stripe_customer_id,
    set_user_plan,
    upsert_subscription,
)
from analytics.metrics import log_event

logger = logging.getLogger(__name__)

_stripe = None


def _get_stripe():
    global _stripe
    if _stripe is None:
        try:
            import stripe
            stripe.api_key = STRIPE_SECRET_KEY
            _stripe = stripe
        except ImportError:
            logger.warning("stripe package not installed — billing disabled.")
            raise
    return _stripe


async def stripe_webhook(request: web.Request) -> web.Response:
    """
    POST /webhook/stripe — Verify signature, check idempotency, dispatch.
    """
    if not STRIPE_SECRET_KEY or not STRIPE_WEBHOOK_SECRET:
        return web.json_response({"error": "Stripe not configured"}, status=503)

    payload = await request.read()
    sig_header = request.headers.get("Stripe-Signature", "")

    try:
        stripe = _get_stripe()
        event = stripe.Webhook.construct_event(
            payload, sig_header, STRIPE_WEBHOOK_SECRET
        )
    except ValueError:
        logger.warning("Stripe webhook: invalid payload.")
        return web.json_response({"error": "Invalid payload"}, status=400)
    except Exception as e:
        logger.warning("Stripe webhook: signature verification failed: %s", e)
        return web.json_response({"error": "Invalid signature"}, status=400)

    event_id = event["id"]
    event_type = event["type"]

    # ── Idempotency check ────────────────────────────────────────────────
    # Stripe retries webhooks on timeout/5xx.  Without this guard, a retry
    # could double-activate a subscription or double-downgrade a user.
    try:
        if is_stripe_event_processed(event_id):
            logger.info("Stripe event %s already processed — skipping.", event_id)
            return web.json_response({"status": "duplicate"})

        # Record BEFORE processing so a crash mid-handler still prevents
        # a duplicate on Stripe's next retry attempt.
        record_stripe_event(event_id, event_type)
    except Exception as exc:
        logger.error("Stripe idempotency check failed: %s — processing anyway.", exc)
        # Fail open: better to risk a duplicate than to silently drop a
        # legitimate payment event.

    logger.info("Stripe event received: %s (id=%s)", event_type, event_id)
    data_object = event["data"]["object"]

    try:
        if event_type == "checkout.session.completed":
            await _handle_checkout_completed(data_object)
        elif event_type == "customer.subscription.deleted":
            await _handle_subscription_deleted(data_object)
        elif event_type == "invoice.payment_failed":
            await _handle_payment_failed(data_object)
        else:
            logger.info("Unhandled Stripe event: %s", event_type)
    except Exception as exc:
        logger.error("Error processing Stripe event %s: %s", event_type, exc)
        return web.json_response({"error": "Processing error"}, status=500)

    return web.json_response({"status": "ok"})


async def _handle_checkout_completed(session: dict) -> None:
    metadata = session.get("metadata", {})
    telegram_id_str = metadata.get("telegram_id")
    if not telegram_id_str:
        logger.warning(
            "checkout.session.completed missing telegram_id in metadata: %s",
            session.get("id"),
        )
        return

    telegram_id = int(telegram_id_str)
    stripe_customer_id = session.get("customer")
    stripe_subscription_id = session.get("subscription")

    set_stripe_customer_id(telegram_id, stripe_customer_id)
    set_user_plan(telegram_id, "pro")

    user = get_user_by_telegram_id(telegram_id)
    if user and stripe_subscription_id:
        upsert_subscription(
            user_id=user["id"],
            stripe_subscription_id=stripe_subscription_id,
            status="active",
        )

    log_event("subscription_created", telegram_id=telegram_id)
    logger.info("User %s upgraded to pro (stripe_customer=%s)", telegram_id, stripe_customer_id)


async def _handle_subscription_deleted(subscription: dict) -> None:
    stripe_customer_id = subscription.get("customer")
    stripe_subscription_id = subscription.get("id")

    user_id = get_user_id_by_stripe_customer(stripe_customer_id)
    if user_id is None:
        logger.warning("subscription.deleted for unknown customer: %s", stripe_customer_id)
        return

    upsert_subscription(
        user_id=user_id,
        stripe_subscription_id=stripe_subscription_id,
        status="canceled",
    )

    telegram_id = get_telegram_id_by_user_id(user_id)
    if telegram_id is not None:
        set_user_plan(telegram_id, "free")
        log_event("subscription_canceled", telegram_id=telegram_id)
        logger.info("User %s downgraded to free.", telegram_id)


async def _handle_payment_failed(invoice: dict) -> None:
    stripe_customer_id = invoice.get("customer")
    stripe_subscription_id = invoice.get("subscription")
    if not stripe_subscription_id:
        return

    user_id = get_user_id_by_stripe_customer(stripe_customer_id)
    if user_id is None:
        return

    upsert_subscription(
        user_id=user_id,
        stripe_subscription_id=stripe_subscription_id,
        status="past_due",
    )

    log_event("payment_failed", stripe_customer=stripe_customer_id)
    logger.warning("Payment failed for customer %s, sub %s", stripe_customer_id, stripe_subscription_id)
