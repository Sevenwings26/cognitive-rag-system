"""FastAPI router for Observatory APM telemetry, hardware inspection, and capacity sizing."""

import logging
from typing import Optional
from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status

from app.dependencies import require_role
from app.schemas.observatory import (
    HostInfoResponse,
    ModelFitResponse,
    ObservatoryMetricsResponse,
    ObservatoryRange,
)
from core.config import settings
from modules.auth.domain.tokens import TokenData
from modules.observatory.service import ObservatoryService, get_observatory_service

logger = logging.getLogger("app.routes.observatory")

router = APIRouter(prefix="/api", tags=["Observatory APM"])

_require_admin = require_role(["SUPER_ADMIN", "DEPT_ADMIN", "ADMIN"])


@router.get(
    "/observatory",
    response_model=ObservatoryMetricsResponse,
    summary="Get live APM and Observatory metrics",
)
def get_observatory_metrics(
    range: ObservatoryRange = Query(
        ObservatoryRange.SEVEN_DAYS,
        description="Time range (15m, 1h, 6h, 24h, Today, 7d, 30d, All_time)",
    ),
    current_user: TokenData = Depends(_require_admin),
):
    service: ObservatoryService = get_observatory_service()
    range_str = range.value if isinstance(range, ObservatoryRange) else str(range)
    return service.get_observatory_metrics(range_key=range_str)


@router.get(
    "/host-info",
    response_model=HostInfoResponse,
    summary="Inspect host hardware topology, GPU devices, and kernel limits",
)
def get_host_info(
    current_user: TokenData = Depends(_require_admin),
):
    service: ObservatoryService = get_observatory_service()
    return service.get_host_info()


@router.get(
    "/autofit",
    response_model=ModelFitResponse,
    summary="Query model auto-fitting recommendations and memory requirements",
)
def evaluate_model_fit(
    model_id: str = Query(
        ...,
        min_length=1,
        max_length=128,
        pattern=r"^[a-zA-Z0-9_\-\.\/]+$",
        description="Target model identifier (e.g. meta-llama/Meta-Llama-3.1-8B-Instruct)",
    ),
    context_window: int = Query(
        8192,
        ge=512,
        le=131072,
        description="Target context length in tokens",
    ),
    concurrency: int = Query(
        4,
        ge=1,
        le=256,
        description="Target concurrent request capacity",
    ),
    force_tp: Optional[int] = Query(
        None,
        ge=1,
        le=32,
        description="Optional forced tensor parallel size",
    ),
    current_user: TokenData = Depends(_require_admin),
):
    service: ObservatoryService = get_observatory_service()
    try:
        return service.evaluate_model_fit(
            model_id=model_id,
            context_window=context_window,
            concurrency=concurrency,
            force_tp=force_tp,
        )
    except ValueError as exc:
        err_msg = str(exc)
        if (
            "not present in the model architecture catalog" in err_msg
            or "Unknown model" in err_msg
            or "not found" in err_msg.lower()
        ):
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=err_msg)
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=err_msg)


@router.post(
    "/observatory/reset",
    summary="Reset in-memory APM buffers and request counters",
)
def reset_observatory_metrics(
    current_user: TokenData = Depends(_require_admin),
):
    service: ObservatoryService = get_observatory_service()
    service.buffer.reset()
    return {"status": "ok", "message": "Observatory metrics reset successfully"}


@router.get(
    "/observatory/metrics",
    summary="Expose Prometheus-compatible plain text metrics",
)
def prometheus_metrics(request: Request):
    token = getattr(settings, "OBSERVATORY_METRICS_SCRAPE_TOKEN", "")
    if token:
        auth_header = request.headers.get("Authorization", "")
        if auth_header != f"Bearer {token}":
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="Invalid metrics scrape token",
            )

    service: ObservatoryService = get_observatory_service()
    content = service.buffer.to_prometheus_text()
    return Response(content=content, media_type="text/plain; version=0.0.4; charset=utf-8")
