"""
analytics/metrics.py — Structured analytics logging.

v5: Added log_generation() for AI generation events with token/cost/latency.
"""

import logging
from datetime import datetime, timezone

logger = logging.getLogger("analytics")


def log_event(event: str, **fields: str | int) -> None:
    """Emit one structured analytics log line."""
    ts = datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
    parts = [f"event={event}"]
    for k, v in fields.items():
        parts.append(f"{k}={v}")
    parts.append(f"ts={ts}")
    logger.info("ANALYTICS | %s", " | ".join(parts))


def log_command(user_id: int, command: str) -> None:
    """Convenience wrapper for command usage events."""
    log_event("command_used", user=user_id, command=command)


def log_generation(
    user_id: int,
    command: str,
    *,
    tokens: int = 0,
    cost_usd: float = 0.0,
    latency_ms: int = 0,
    status: str = "success",
) -> None:
    """
    Log an AI generation event with token usage and cost.

    Format:
        ANALYTICS | event=generation | user=123 | command=post | tokens=450
                  | cost=0.00032 | latency_ms=1843 | status=success | ts=...
    """
    log_event(
        "generation",
        user=user_id,
        command=command,
        tokens=tokens,
        cost=f"{cost_usd:.5f}",
        latency_ms=latency_ms,
        status=status,
    )
