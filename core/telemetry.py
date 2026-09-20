"""Engine-neutral, optional telemetry port owned by the application kernel."""

from __future__ import annotations

import logging
import threading
import time
from contextlib import contextmanager
from enum import Enum
from typing import Any, Callable, Dict, Iterator, Optional


logger = logging.getLogger("core.telemetry")


class OperationTimer:
    """Accumulate diagnostic timings for named application stages."""

    def __init__(self) -> None:
        self.timings: Dict[str, float] = {}

    @contextmanager
    def record(self, stage_name: str) -> Iterator[None]:
        start = time.perf_counter()
        try:
            yield
        finally:
            self.timings[stage_name] = round(
                (time.perf_counter() - start) * 1000.0, 2
            )

    def get(self, stage_name: str, default: float = 0.0) -> float:
        return self.timings.get(stage_name, default)

    def format_summary(self) -> str:
        parts = [f"{key}={value:.1f}ms" for key, value in self.timings.items()]
        return " | ".join(parts) if parts else "no_timings"


class BackendRoute(str, Enum):
    RAG = "rag"
    SQL = "sql"
    GENERAL = "general"
    ATTACHMENTS = "attachments"
    DENIED = "denied"
    CLASSIFICATION_FAILURE = "classification_failure"
    UNMAPPED = "unmapped"
    SCANNER = "scanner"

    def __str__(self) -> str:
        return self.value


_VALID_ROUTE_VALUES = frozenset(route.value for route in BackendRoute)


def normalize_backend_route(value: Any) -> BackendRoute:
    if isinstance(value, BackendRoute):
        return value
    if not isinstance(value, str):
        return BackendRoute.UNMAPPED

    cleaned = value.strip().lower()
    if cleaned in _VALID_ROUTE_VALUES:
        return BackendRoute(cleaned)
    aliases = {
        "rag_retrieval": BackendRoute.RAG,
        "retrieval": BackendRoute.RAG,
        "vector": BackendRoute.RAG,
        "doc": BackendRoute.RAG,
        "database": BackendRoute.SQL,
        "external_db": BackendRoute.SQL,
        "structured_sql": BackendRoute.SQL,
        "personal_attachment": BackendRoute.ATTACHMENTS,
        "attachment": BackendRoute.ATTACHMENTS,
        "files": BackendRoute.ATTACHMENTS,
        "forbidden": BackendRoute.DENIED,
        "unauthorized": BackendRoute.DENIED,
        "access_denied": BackendRoute.DENIED,
        "archived_session": BackendRoute.DENIED,
        "error": BackendRoute.CLASSIFICATION_FAILURE,
        "router_error": BackendRoute.CLASSIFICATION_FAILURE,
        "intent_error": BackendRoute.CLASSIFICATION_FAILURE,
        "personal": BackendRoute.GENERAL,
        "direct": BackendRoute.GENERAL,
        "greeting": BackendRoute.GENERAL,
        "conversational": BackendRoute.GENERAL,
    }
    return aliases.get(cleaned, BackendRoute.UNMAPPED)


_recorder_lock = threading.Lock()
_route_recorder: Optional[Callable[[BackendRoute], None]] = None


def bind_backend_route_recorder(
    recorder: Optional[Callable[[BackendRoute], None]],
) -> None:
    global _route_recorder
    with _recorder_lock:
        _route_recorder = recorder


def record_backend_route(route: BackendRoute | str) -> None:
    canonical = normalize_backend_route(route)
    with _recorder_lock:
        recorder = _route_recorder
    if recorder is None:
        return
    try:
        recorder(canonical)
    except Exception as exc:
        logger.debug("Optional backend route telemetry failed: %s", exc)
