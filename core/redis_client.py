"""Centralized Redis connection client with graceful fallback."""

import logging
from typing import Optional
import redis
from core.config import settings

logger = logging.getLogger("core.redis_client")

_REDIS_CLIENT: Optional[redis.Redis] = None
_REDIS_CHECKED: bool = False


def get_redis() -> Optional[redis.Redis]:
    """Return an authoritative Redis client or None if Redis is unreachable."""
    global _REDIS_CLIENT, _REDIS_CHECKED
    if _REDIS_CHECKED:
        return _REDIS_CLIENT

    redis_url = getattr(settings, "REDIS_URL", "redis://redis:6379/0")
    if not redis_url:
        _REDIS_CHECKED = True
        return None

    try:
        client = redis.from_url(
            redis_url,
            decode_responses=True,
            socket_timeout=1.5,
            socket_connect_timeout=1.5,
        )
        client.ping()
        _REDIS_CLIENT = client
        logger.info("Connected to Redis at %s", redis_url)
    except Exception as exc:
        logger.warning("Redis is unreachable (%s). Falling back to in-memory mode.", exc)
        _REDIS_CLIENT = None
    finally:
        _REDIS_CHECKED = True

    return _REDIS_CLIENT
