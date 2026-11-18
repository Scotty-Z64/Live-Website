"""Redis pub/sub fan-out for the WebSocket Action Drawer."""
from __future__ import annotations

import json
import logging
from functools import lru_cache

import redis

from ..config import get_settings

log = logging.getLogger(__name__)


@lru_cache
def get_redis() -> redis.Redis:
    return redis.Redis.from_url(get_settings().redis_url, decode_responses=True)


def bid_channel(tenant_id, bid_id) -> str:  # noqa: ANN001
    return f"ts:bid:{tenant_id}:{bid_id}"


def tenant_channel(tenant_id) -> str:  # noqa: ANN001
    return f"ts:tenant:{tenant_id}"


def publish(channel: str, event_type: str, payload: dict) -> None:
    try:
        get_redis().publish(channel, json.dumps({"type": event_type, "payload": payload}, default=str))
    except redis.RedisError as exc:  # events are best-effort; state lives in Postgres
        log.warning("event publish failed on %s: %s", channel, exc)
