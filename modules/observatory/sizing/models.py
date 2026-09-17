"""Data models and enums for model capacity planning and sizing."""

from dataclasses import dataclass, field, asdict
from enum import Enum
from typing import Dict, List, Optional, Any


class PrecisionType(str, Enum):
    FP16 = "float16"
    BF16 = "bfloat16"
    FP8 = "fp8"
    AWQ_4BIT = "awq"
    GPTQ_4BIT = "gptq"
    GGUF_Q4_K_M = "q4_k_m"


class FitStatus(str, Enum):
    FITS = "FITS"
    FITS_WITH_QUANT = "FITS_WITH_QUANT"
    FITS_WITH_TP = "FITS_WITH_TP"
    OOM = "OOM"


@dataclass
class ModelSpec:
    model_id: str
    display_name: str
    parameter_count_b: float
    num_layers: int
    hidden_size: int
    num_attention_heads: int
    num_kv_heads: int
    head_dim: int
    hf_repo_id: str = ""
    ollama_model_id: str = ""
    exact_model_revision: str = "main"
    available_weight_formats: List[str] = field(default_factory=lambda: ["safetensors"])
    registered_quantizations: Dict[str, str] = field(default_factory=dict)
    max_context_len: int = 32768
    default_context_len: int = 8192
    supported_precisions: List[PrecisionType] = field(default_factory=lambda: [
        PrecisionType.BF16, PrecisionType.FP16, PrecisionType.AWQ_4BIT, PrecisionType.GPTQ_4BIT
    ])

    def __post_init__(self):
        if not self.hf_repo_id:
            self.hf_repo_id = self.model_id
        if not self.ollama_model_id:
            parts = self.model_id.split("/")
            self.ollama_model_id = parts[-1].lower()

    @property
    def is_gqa(self) -> bool:
        return self.num_kv_heads < self.num_attention_heads


@dataclass
class MemoryBudget:
    weights_unquantized_gb: float
    weights_quantized_gb: float
    kv_cache_gb: float
    runtime_overhead_gb: float
    total_required_vram_gb: float
    headroom_vram_gb: float
    cuda_overhead_gb: float = 0.0
    memory_type: str = "vram"


@dataclass
class DeploymentPlan:
    model_id: str
    fit_status: FitStatus
    recommended_quantization: str
    recommended_dtype: str
    recommended_tensor_parallel: int
    max_safe_concurrency: int
    vllm_command_template: str
    scaling_advice: str
    launch_arguments: List[str] = field(default_factory=list)
    launch_config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class ValidatedDeploymentPlan:
    engine: str
    engine_specific_model_id: str
    exact_model_revision: str
    available_weight_format: str
    quantization_artifact: Optional[str]
    gpu_count: int
    per_device_memory_gb: List[float]
    topology: str
    supported_dtype: str
    supported_tensor_parallel: int
    model_divisibility_constraints: Dict[str, Any]
    max_safe_concurrency: int
    deployment_command: str
    launch_arguments: List[str] = field(default_factory=list)
    launch_config: Dict[str, Any] = field(default_factory=dict)


@dataclass
class FitEvaluationResult:
    model_id: str
    fit_status: FitStatus
    hardware_evaluated: Dict[str, Any]
    memory_budget: MemoryBudget
    recommendation: Optional[DeploymentPlan] = None
    deployment_plan: Optional[ValidatedDeploymentPlan] = None
    missing_prerequisites: List[str] = field(default_factory=list)
    scaling_advice: str = ""
    is_capacity_estimate_only: bool = False
