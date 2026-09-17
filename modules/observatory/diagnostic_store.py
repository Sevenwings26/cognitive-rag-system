"""Bounded Redis Stream adapter for permission-scoped diagnostic events."""

from __future__ import annotations

import collections
import json
import logging
import threading
import time
from typing import Callable, Iterable

from core.config import settings
from core.diagnostics import DiagnosticAudience, DiagnosticEvent
from core.redis_client import get_redis


logger = logging.getLogger(__name__)
_MAX_QUERY_LIMIT = 500
_MAX_SCAN_EVENTS = 5_000


class DiagnosticStore:
    """Cross-process event storage with a bounded process-local fallback."""

    def __init__(
        self,
        *,
        redis_provider: Callable[[], object] = get_redis,
        prefix: str | None = None,
        max_events: int | None = None,
        retention_seconds: int | None = None,
        fallback_max_events: int | None = None,
    ) -> None:
        self._redis_provider = redis_provider
        self._prefix = prefix or settings.diagnostics_redis_prefix
        self._max_events = max_events or settings.diagnostics_stream_max_events
        self._retention_seconds = retention_seconds or settings.diagnostics_retention_seconds
        fallback_size = fallback_max_events or settings.diagnostics_fallback_max_events
        self._fallback: collections.deque[DiagnosticEvent] = collections.deque(maxlen=fallback_size)
        self._lock = threading.RLock()
        self._redis_available = False

    def _global_key(self) -> str:
        return f"{self._prefix}:all"

    def _user_key(self, user_id: int) -> str:
        return f"{self._prefix}:user:{int(user_id)}"

    def _department_key(self, department_id: int) -> str:
        return f"{self._prefix}:department:{int(department_id)}"

    def _target_keys(self, event: DiagnosticEvent) -> list[str]:
        keys = [self._global_key()]
        if event.audience is DiagnosticAudience.USER and event.context.user_id is not None:
            keys.append(self._user_key(event.context.user_id))
            keys.extend(
                self._department_key(value)
                for value in event.context.department_ids
            )
        elif event.audience is DiagnosticAudience.DEPARTMENT:
            keys.extend(self._department_key(value) for value in event.context.department_ids)
        return list(dict.fromkeys(keys))

    def record(self, event: DiagnosticEvent) -> None:
        """Store safely; Redis failure never interrupts the originating request."""
        if not settings.diagnostics_enabled:
            return
        with self._lock:
            self._fallback.append(event)
        payload = json.dumps(event.to_record(), separators=(",", ":"), sort_keys=True)
        try:
            client = self._redis_provider()
            oldest_stream_id = f"{max(0, int((time.time() - self._retention_seconds) * 1000))}-0"
            for key in self._target_keys(event):
                client.xadd(
                    key,
                    {"event": payload},
                    maxlen=self._max_events,
                    approximate=True,
                )
                client.xtrim(key, minid=oldest_stream_id, approximate=True)
                client.expire(key, self._retention_seconds)
            self._redis_available = True
        except Exception as exc:
            self._redis_available = False
            logger.debug("Diagnostic Redis write unavailable: %s", type(exc).__name__)

    @staticmethod
    def _validate_limit(limit: int) -> int:
        value = int(limit)
        if value < 1 or value > _MAX_QUERY_LIMIT:
            raise ValueError(f"diagnostic query limit must be between 1 and {_MAX_QUERY_LIMIT}")
        return value

    def _read_redis(self, key: str, limit: int) -> list[DiagnosticEvent] | None:
        try:
            rows = self._redis_provider().xrevrange(key, count=min(_MAX_SCAN_EVENTS, max(limit * 10, limit)))
            events: list[DiagnosticEvent] = []
            for _stream_id, fields in rows:
                raw = fields.get("event")
                if not raw:
                    continue
                try:
                    events.append(DiagnosticEvent.from_record(json.loads(raw)))
                except (KeyError, TypeError, ValueError, json.JSONDecodeError):
                    logger.warning("Discarded malformed structured diagnostic event")
            self._redis_available = True
            return events
        except Exception as exc:
            self._redis_available = False
            logger.debug("Diagnostic Redis read unavailable: %s", type(exc).__name__)
            return None

    def _read_fallback(self) -> list[DiagnosticEvent]:
        with self._lock:
            return list(reversed(self._fallback))

    @staticmethod
    def _filter(
        events: Iterable[DiagnosticEvent],
        *,
        audiences: frozenset[DiagnosticAudience],
        user_id: int | None = None,
        department_ids: frozenset[int] = frozenset(),
        conversation_id: str | None = None,
        limit: int,
    ) -> list[DiagnosticEvent]:
        result: list[DiagnosticEvent] = []
        for event in events:
            if event.audience not in audiences:
                continue
            if user_id is not None and event.context.user_id != user_id:
                continue
            if department_ids and not department_ids.intersection(event.context.department_ids):
                continue
            if conversation_id and event.context.conversation_id != conversation_id:
                continue
            result.append(event)
            if len(result) >= limit:
                break
        return result

    def _events_for_key(self, key: str, limit: int) -> tuple[list[DiagnosticEvent], str]:
        events = self._read_redis(key, limit)
        if events is not None:
            return events, "redis"
        return self._read_fallback(), "single_process_fallback"

    def list_for_user(
        self,
        user_id: int,
        *,
        conversation_id: str | None = None,
        limit: int = 200,
    ) -> tuple[list[DiagnosticEvent], str]:
        limit = self._validate_limit(limit)
        events, mode = self._events_for_key(self._user_key(user_id), limit)
        return self._filter(
            events,
            audiences=frozenset({DiagnosticAudience.USER}),
            user_id=int(user_id),
            conversation_id=conversation_id,
            limit=limit,
        ), mode

    def list_for_departments(
        self,
        department_ids: Iterable[int],
        *,
        limit: int = 200,
    ) -> tuple[list[DiagnosticEvent], str]:
        limit = self._validate_limit(limit)
        allowed = frozenset(int(value) for value in department_ids)
        if not allowed:
            return [], "redis" if self._redis_available else "single_process_fallback"
        collected: dict[str, DiagnosticEvent] = {}
        modes: set[str] = set()
        for department_id in sorted(allowed):
            events, mode = self._events_for_key(self._department_key(department_id), limit)
            modes.add(mode)
            for event in self._filter(
                events,
                audiences=frozenset({
                    DiagnosticAudience.USER,
                    DiagnosticAudience.DEPARTMENT,
                }),
                department_ids=allowed,
                limit=limit,
            ):
                collected[event.event_id] = event
        ordered = sorted(collected.values(), key=lambda item: item.occurred_at, reverse=True)
        mode = "redis" if modes == {"redis"} else "single_process_fallback"
        return ordered[:limit], mode

    def list_for_super_admin(self, *, limit: int = 200) -> tuple[list[DiagnosticEvent], str]:
        limit = self._validate_limit(limit)
        events, mode = self._events_for_key(self._global_key(), limit)
        return events[:limit], mode


_store: DiagnosticStore | None = None
_store_lock = threading.Lock()


def get_diagnostic_store() -> DiagnosticStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = DiagnosticStore()
    return _store
