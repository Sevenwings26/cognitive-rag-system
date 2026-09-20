"""Pydantic response models for APM and Observability endpoints."""

from enum import Enum
from typing import Any, Dict, List, Optional
from pydantic import BaseModel, Field


class ObservatoryRange(str, Enum):
    FIFTEEN_MIN = "15m"
    ONE_HOUR = "1h"
    SIX_HOURS = "6h"
    TWENTY_FOUR_HOURS = "24h"
    TODAY = "Today"
    SEVEN_DAYS = "7d"
    THIRTY_DAYS = "30d"
    ALL_TIME = "All_time"


class ApplicationLogEntry(BaseModel):
    timestamp: str
    level: str
    logger: str
    user_email: str = ""
    message: str
    source: str


class ApplicationLogCounts(BaseModel):
    debug: int = 0
    info: int = 0
    warning: int = 0
    error: int = 0
    critical: int = 0


class ApplicationLogsResponse(BaseModel):
    entries: List[ApplicationLogEntry]
    total: int
    offset: int
    limit: int
    has_more: bool
    counts: ApplicationLogCounts
    source: str
    generated_at: str
    coverage: str


class TelemetryStatus(BaseModel):
    aggregation_mode: str = Field(default="single_process_fallback", description="'redis' or 'single_process_fallback'")
    data_status: str = Field(default="available", description="'available', 'partial', 'degraded', 'unavailable', or 'single_process_fallback'")
    retention: str = Field(default="30d", description="Retention policy (e.g. 30d)")
    timezone: str = Field(default="UTC", description="Timestamp timezone (UTC)")
    concurrency_estimated: bool = Field(default=False, description="True if concurrency is estimated")
    worker_pid: int = Field(default=0, description="PID of worker answering query")
    message: Optional[str] = Field(default=None, description="Informational warning or fallback details")


class KpiMetrics(BaseModel):
    uptime_seconds: float
    uptime_human: str
    total_requests: int
    user_requests: int = 0
    system_requests: int = 0
    concurrency_current: int
    avg_response_time_sec: Optional[float] = None
    p95_response_time_sec: Optional[float] = None
    app_error_rate_pct: Optional[float] = None
    ext_error_rate_pct: Optional[float] = None
    active_sessions: Optional[int] = None


class HostCpuGauge(BaseModel):
    percent: Optional[float] = None
    cores: int
    load_average_1m: Optional[float] = None
    status: str = "available"


class HostMemoryGauge(BaseModel):
    percent: Optional[float] = None
    used_gb: Optional[float] = None
    total_gb: Optional[float] = None
    status: str = "available"


class BackendSplitGauge(BaseModel):
    rag_percent: Optional[float] = None
    sql_percent: Optional[float] = None
    rag_total: int
    sql_total: int
    general_total: int = 0
    attachments_total: int = 0
    denied_total: int = 0
    classification_failure_total: int = 0


class HostGpuMemoryGauge(BaseModel):
    gpu_memory_used_bytes: Optional[int] = None
    gpu_memory_total_bytes: Optional[int] = None
    gpu_memory_utilization_percent: Optional[float] = None
    status: str = "available"


class HostGpuComputeGauge(BaseModel):
    gpu_compute_utilization_percent: Optional[float] = None
    status: str = "available"


class ObservatoryGauges(BaseModel):
    host_cpu: HostCpuGauge
    host_memory: HostMemoryGauge
    backend_split_total: BackendSplitGauge
    gpu_memory: Optional[HostGpuMemoryGauge] = None
    gpu_compute: Optional[HostGpuComputeGauge] = None


class GpuServiceAllocation(BaseModel):
    service: str
    allocated_gb: float
    used_gb: float
    unit: str = "GB"


class BackendSplitPoint(BaseModel):
    timestamp: str
    rag: int
    sql: int
    general: int = 0
    attachments: int = 0


class ResponseTimePoint(BaseModel):
    timestamp: str
    sample_count: int = 1
    avg: Optional[float] = None
    p95: Optional[float] = None


class ConcurrencyPoint(BaseModel):
    timestamp: str
    concurrency: int


class HostUtilizationPoint(BaseModel):
    timestamp: str
    cpu_pct: Optional[float] = None
    gpu_memory_used_bytes: Optional[int] = None
    gpu_memory_total_bytes: Optional[int] = None
    gpu_memory_utilization_percent: Optional[float] = None
    gpu_compute_utilization_percent: Optional[float] = None
    gpu_pct: Optional[float] = None
    gpu_name: Optional[str] = None
    status: str = "available"


class ObservatoryTimeSeries(BaseModel):
    backend_split_over_time: List[BackendSplitPoint]
    response_time_over_time: List[ResponseTimePoint]
    concurrency_over_time: List[ConcurrencyPoint]
    host_utilization_over_time: List[HostUtilizationPoint]


class ObservatoryMetricsResponse(BaseModel):
    status: str = "ok"
    range: str
    generated_at: str
    telemetry_status: Optional[TelemetryStatus] = None
    kpis: KpiMetrics
    gauges: ObservatoryGauges
    gpu_resource_allocation: List[GpuServiceAllocation]
    time_series: ObservatoryTimeSeries


class GpuDeviceDetail(BaseModel):
    slot: str
    vendor: str
    model_name: str
    vram_total_gb: float
    vram_available_gb: float
    compute_capability: str
    driver_version: str
    uuid: Optional[str] = None
    memory_type: str = "discrete"
    architecture: str = "unknown"
    discrete_vram_total_gb: float = 0.0
    discrete_vram_available_gb: float = 0.0
    unified_memory_pool_gb: Optional[float] = None
    host_ram_available_gb: Optional[float] = None
    estimated_usable_memory_gb: Optional[float] = None
    detection_confidence: str = "authoritative"
    gpu_memory_used_bytes: Optional[int] = None
    gpu_memory_total_bytes: Optional[int] = None
    gpu_memory_utilization_percent: Optional[float] = None
    gpu_compute_utilization_percent: Optional[float] = None


class HostInfoGpu(BaseModel):
    gpu_available: bool
    total_gpu_count: int
    detection_method: str
    devices: List[GpuDeviceDetail]


class HostInfoCpu(BaseModel):
    model_name: str
    physical_cores: int
    logical_threads: int
    architecture: str
    avx2_supported: bool
    avx512_supported: bool
    amx_supported: bool = False
    vnni_supported: bool = False


class HostInfoMemory(BaseModel):
    total_ram_gb: float
    available_ram_gb: float
    used_ram_gb: float
    swap_total_gb: float
    swap_free_gb: float


class HostInfoStorage(BaseModel):
    mount: str
    total_gb: float
    free_gb: float
    used_gb: float


class HostInfoKernel(BaseModel):
    vm_max_map_count: int
    vm_max_map_count_passed: bool
    ulimit_nofile_soft: int
    thp_enabled: str


class HostInfoOs(BaseModel):
    system: str
    release: str
    machine: str
    is_wsl: bool
    is_container: bool


class HostInfoResponse(BaseModel):
    status: str = "ok"
    host_name: str
    inference_engine: str
    os: HostInfoOs
    cpu: HostInfoCpu
    memory: HostInfoMemory
    gpu: HostInfoGpu
    storage: HostInfoStorage
    kernel: HostInfoKernel


class MemoryBudgetSchema(BaseModel):
    weights_unquantized_gb: float
    weights_quantized_gb: float
    kv_cache_gb: float
    runtime_overhead_gb: float = Field(default=0.0)
    cuda_overhead_gb: float = Field(default=0.0)
    total_required_vram_gb: float
    headroom_vram_gb: float
    memory_type: str = Field(default="vram")


class DeploymentPlanSchema(BaseModel):
    optimal_quantization: str
    optimal_dtype: str
    recommended_tensor_parallel: int
    max_safe_concurrency: int
    vllm_command_template: str
    scaling_advice: str
    launch_arguments: List[str] = []
    launch_config: Dict[str, Any] = {}


class ValidatedDeploymentPlanSchema(BaseModel):
    engine: str
    engine_specific_model_id: str
    exact_model_revision: Optional[str] = None
    available_weight_format: str
    quantization_artifact: Optional[str] = None
    gpu_count: int
    per_device_memory_gb: List[float] = []
    topology: str
    supported_dtype: str
    supported_tensor_parallel: int
    model_divisibility_constraints: Dict[str, Any] = {}
    max_safe_concurrency: int
    deployment_command: str
    launch_arguments: List[str] = []
    launch_config: Dict[str, Any] = {}


class ModelFitResponse(BaseModel):
    status: str = "ok"
    model_id: str
    fit_status: str
    hardware_evaluated: Dict[str, Any]
    memory_budget: MemoryBudgetSchema
    recommendation: Optional[DeploymentPlanSchema] = None
    deployment_plan: Optional[ValidatedDeploymentPlanSchema] = None
    missing_prerequisites: List[str] = []
    scaling_advice: str
    is_capacity_estimate_only: bool = False

class AlertItem(BaseModel):
    status: str = Field(..., pattern=r"^(firing|resolved)$", description="Alert state: 'firing' or 'resolved'")
    labels: Dict[str, str] = Field(default_factory=dict, description="Alert labels")
    annotations: Dict[str, str] = Field(default_factory=dict, description="Alert annotations")
    startsAt: Optional[str] = Field(None, max_length=64)
    endsAt: Optional[str] = Field(None, max_length=64)
    generatorURL: Optional[str] = Field(None, max_length=1024)
    fingerprint: Optional[str] = Field(None, max_length=128)


class AlertmanagerWebhookPayload(BaseModel):
    version: Optional[str] = Field(None, max_length=16)
    groupKey: Optional[str] = Field(None, max_length=256)
    truncatedAlerts: Optional[int] = Field(0, ge=0)
    status: str = Field(..., pattern=r"^(firing|resolved)$", description="Overall alert notification status")
    receiver: str = Field(..., max_length=128, description="Target receiver name")
    groupLabels: Dict[str, str] = Field(default_factory=dict)
    commonLabels: Dict[str, str] = Field(default_factory=dict)
    commonAnnotations: Dict[str, str] = Field(default_factory=dict)
    externalURL: Optional[str] = Field(None, max_length=1024)
    alerts: List[AlertItem] = Field(default_factory=list, max_length=100)


class WebhookResponseSchema(BaseModel):
    status: str = "ok"
    received_alerts: int
    active_alerts_count: int
