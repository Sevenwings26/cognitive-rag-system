"""Model auto-fitting, VRAM capacity calculation, and sizing engine."""

from .models import ModelSpec, PrecisionType, FitStatus, FitEvaluationResult, DeploymentPlan
from .catalog import get_model_spec, list_supported_models
from .sizer import ModelSizer
from .allocator import GPUAllocator

__all__ = [
    "ModelSpec",
    "PrecisionType",
    "FitStatus",
    "FitEvaluationResult",
    "DeploymentPlan",
    "get_model_spec",
    "list_supported_models",
    "ModelSizer",
    "GPUAllocator",
]
