"""FastAPI ASGI middleware for APM request latency, concurrency, and route metrics.
Distinguishes real user business transactions from synthetic/system telemetry and static assets.
"""

import time
import uuid
from typing import Any, Awaitable, Callable, Dict

from core.config import settings
from modules.observatory.buffer import get_observatory_buffer

# System/telemetry endpoints categorized as synthetic/system
_SYSTEM_PREFIXES = (
    "/api/observatory",
    "/api/host-info",
    "/api/autofit",
    "/metrics",
    "/health",
)
_SYSTEM_SUFFIXES = (
    "/metrics",
    "/health",
)

# Static assets and UI pages to ignore from API transaction metrics
_STATIC_SUFFIXES = (
    ".js",
    ".css",
    ".png",
    ".jpg",
    ".jpeg",
    ".svg",
    ".ico",
    ".woff",
    ".woff2",
    ".map",
    ".html",
)
_STATIC_PATHS = (
    "/",
    "/favicon.ico",
    "/enterprise/dashboard",
    "/enterprise/observatory",
    "/enterprise/login",
    "/login",
)


def _classify_traffic(path: str) -> str:
    """Classify traffic into 'system', 'static', or 'user'."""
    lowered = path.lower()
    if lowered.startswith(_SYSTEM_PREFIXES) or lowered.endswith(_SYSTEM_SUFFIXES):
        return "system"
    if lowered.endswith(_STATIC_SUFFIXES) or lowered in _STATIC_PATHS:
        return "static"
    return "user"


class ObservatoryMiddleware:
    """ASGI request timer that observes the complete response body.

    ``BaseHTTPMiddleware.call_next()`` returns when a streaming response starts,
    which records time-to-first-byte and releases concurrency too early. Wrapping
    the ASGI ``send`` callable keeps the request active until the response body is
    complete without buffering or changing delivery semantics.
    """

    def __init__(self, app):
        self.app = app
        self.buffer = get_observatory_buffer()

    async def __call__(
        self,
        scope: Dict[str, Any],
        receive: Callable[[], Awaitable[Dict[str, Any]]],
        send: Callable[[Dict[str, Any]], Awaitable[None]],
    ) -> None:
        if scope.get("type") != "http" or not settings.observatory_enabled:
            await self.app(scope, receive, send)
            return

        path = str(scope.get("path") or "")
        traffic_kind = _classify_traffic(path)
        is_user = (traffic_kind == "user")
        is_system = (traffic_kind == "system")

        start_time = time.perf_counter()
        state = scope.get("state") or {}
        request_id = state.get("request_id") or uuid.uuid4().hex[:16]

        # In-flight concurrency: only track real user business transactions
        if is_user:
            self.buffer.increment_concurrency(request_id)

        status_code = 500

        async def observe_send(message: Dict[str, Any]) -> None:
            nonlocal status_code
            if message.get("type") == "http.response.start":
                status_code = int(message.get("status", 500))
            await send(message)

        try:
            await self.app(scope, receive, observe_send)
        finally:
            if is_user:
                self.buffer.decrement_concurrency(request_id)
            elapsed_sec = time.perf_counter() - start_time

            # Only record user transactions and synthetic system probes (skip static file downloads & reset commands)
            if traffic_kind in ("user", "system") and not path.endswith("/observatory/reset"):
                self.buffer.record_request(
                    path=path,
                    duration_sec=elapsed_sec,
                    status_code=status_code,
                    route_type="denied" if status_code in (401, 403) else None,
                    is_system=is_system,
                    traffic_kind=traffic_kind,
                    request_id=request_id if is_user else None,
                )
