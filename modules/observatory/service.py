"""
ObservatoryService: Facade orchestrating live metrics, host inspection, and model fitting.
"""

import logging
import socket
from typing import Any, Dict, Optional
from core.config import settings
from .buffer import get_observatory_buffer, ObservatoryBuffer
from .probes.cpu import CPUInspector
from .probes.gpu import GPUInspector
from .probes.memory import MemoryInspector
from .probes.kernel import KernelInspector
from .probes.storage import StorageInspector
from .probes.platform import PlatformInspector
from .sizing.sizer import ModelSizer
from .sizing.models import FitEvaluationResult

logger = logging.getLogger("techyz.observatory.service")


class ObservatoryService:
    """Unified service layer for APM observatory endpoints."""

    def __init__(self):
        self.buffer: ObservatoryBuffer = get_observatory_buffer()
        self.cpu_inspector = CPUInspector()
        self.gpu_inspector = GPUInspector()
        self.memory_inspector = MemoryInspector()
        self.kernel_inspector = KernelInspector()
        self.storage_inspector = StorageInspector()
        self.platform_inspector = PlatformInspector()
        self.model_sizer = ModelSizer()

    def get_observatory_metrics(self, range_key: str = "7d") -> Dict[str, Any]:
        # Determine real active sessions from Redis token registry
        active_count = self._count_active_sessions()
        return self.buffer.get_metrics(range_key=range_key, active_sessions_count=active_count)

    def get_host_info(self) -> Dict[str, Any]:
        plat = self.platform_inspector.probe()
        cpu = self.cpu_inspector.probe()
        mem = self.memory_inspector.probe()
        gpu = self.gpu_inspector.probe()
        storage = self.storage_inspector.probe("/")
        kernel = self.kernel_inspector.probe()

        host_name = socket.gethostname()

        return {
            "status": "ok",
            "host_name": host_name,
            "inference_engine": settings.inference_engine,
            "os": {
                "system": plat.system,
                "release": plat.release,
                "machine": plat.machine,
                "is_wsl": plat.is_wsl,
                "is_container": plat.is_container,
            },
            "cpu": {
                "model_name": cpu.model_name,
                "physical_cores": cpu.physical_cores,
                "logical_threads": cpu.logical_threads,
                "architecture": cpu.architecture,
                "avx2_supported": cpu.avx2_supported,
                "avx512_supported": cpu.avx512_supported,
                "amx_supported": getattr(cpu, "amx_supported", False),
                "vnni_supported": getattr(cpu, "vnni_supported", False),
            },
            "memory": {
                "total_ram_gb": mem.total_ram_gb,
                "available_ram_gb": mem.available_ram_gb,
                "used_ram_gb": mem.used_ram_gb,
                "swap_total_gb": mem.swap_total_gb,
                "swap_free_gb": mem.swap_free_gb,
            },
            "gpu": {
                "gpu_available": gpu.gpu_available,
                "total_gpu_count": gpu.total_gpu_count,
                "detection_method": gpu.detection_method,
                "devices": [
                    {
                        "slot": g.slot,
                        "vendor": g.vendor,
                        "model_name": g.model_name,
                        "vram_total_gb": g.vram_total_gb,
                        "vram_available_gb": g.vram_available_gb,
                        "compute_capability": g.compute_capability,
                        "driver_version": g.driver_version,
                        "uuid": g.uuid,
                        "memory_type": getattr(g, "memory_type", "discrete"),
                        "architecture": getattr(g, "architecture", "unknown"),
                        "discrete_vram_total_gb": getattr(g, "discrete_vram_total_gb", 0.0),
                        "discrete_vram_available_gb": getattr(g, "discrete_vram_available_gb", 0.0),
                        "unified_memory_pool_gb": getattr(g, "unified_memory_pool_gb", None),
                        "host_ram_available_gb": getattr(g, "host_ram_available_gb", None),
                        "estimated_usable_memory_gb": getattr(g, "estimated_usable_memory_gb", None),
                        "detection_confidence": getattr(g, "detection_confidence", "authoritative"),
                    }
                    for g in gpu.gpus
                ],
            },
            "storage": {
                "mount": storage.mount_point,
                "total_gb": storage.total_gb,
                "free_gb": storage.free_gb,
                "used_gb": storage.used_gb,
            },
            "kernel": {
                "vm_max_map_count": kernel.vm_max_map_count,
                "vm_max_map_count_passed": kernel.vm_max_map_count_passed,
                "ulimit_nofile_soft": kernel.ulimit_nofile_soft,
                "thp_enabled": kernel.thp_enabled,
            },
        }

    def evaluate_model_fit(
        self,
        model_id: str,
        context_window: int = 8192,
        concurrency: int = 4,
        force_tp: Optional[int] = None,
    ) -> Dict[str, Any]:
        res: FitEvaluationResult = self.model_sizer.evaluate(
            model_id=model_id,
            context_window=context_window,
            concurrency=concurrency,
            force_tp=force_tp,
        )

        return {
            "status": "ok",
            "model_id": res.model_id,
            "fit_status": res.fit_status.value,
            "hardware_evaluated": res.hardware_evaluated,
            "memory_budget": {
                "weights_unquantized_gb": res.memory_budget.weights_unquantized_gb,
                "weights_quantized_gb": res.memory_budget.weights_quantized_gb,
                "kv_cache_gb": res.memory_budget.kv_cache_gb,
                "runtime_overhead_gb": res.memory_budget.runtime_overhead_gb,
                "cuda_overhead_gb": res.memory_budget.cuda_overhead_gb,
                "total_required_vram_gb": res.memory_budget.total_required_vram_gb,
                "headroom_vram_gb": res.memory_budget.headroom_vram_gb,
                "memory_type": res.memory_budget.memory_type,
            },
            "recommendation": {
                "optimal_quantization": res.recommendation.recommended_quantization,
                "optimal_dtype": res.recommendation.recommended_dtype,
                "recommended_tensor_parallel": res.recommendation.recommended_tensor_parallel,
                "max_safe_concurrency": res.recommendation.max_safe_concurrency,
                "vllm_command_template": res.recommendation.vllm_command_template,
                "scaling_advice": res.recommendation.scaling_advice,
                "launch_arguments": res.recommendation.launch_arguments,
                "launch_config": res.recommendation.launch_config,
            } if res.recommendation else None,
            "deployment_plan": {
                "engine": res.deployment_plan.engine,
                "engine_specific_model_id": res.deployment_plan.engine_specific_model_id,
                "exact_model_revision": res.deployment_plan.exact_model_revision,
                "available_weight_format": res.deployment_plan.available_weight_format,
                "quantization_artifact": res.deployment_plan.quantization_artifact,
                "gpu_count": res.deployment_plan.gpu_count,
                "per_device_memory_gb": res.deployment_plan.per_device_memory_gb,
                "topology": res.deployment_plan.topology,
                "supported_dtype": res.deployment_plan.supported_dtype,
                "supported_tensor_parallel": res.deployment_plan.supported_tensor_parallel,
                "model_divisibility_constraints": res.deployment_plan.model_divisibility_constraints,
                "max_safe_concurrency": res.deployment_plan.max_safe_concurrency,
                "deployment_command": res.deployment_plan.deployment_command,
                "launch_arguments": res.deployment_plan.launch_arguments,
                "launch_config": res.deployment_plan.launch_config,
            } if res.deployment_plan else None,
            "missing_prerequisites": res.missing_prerequisites,
            "scaling_advice": res.scaling_advice,
            "is_capacity_estimate_only": res.is_capacity_estimate_only,
        }

    def _count_active_sessions(self) -> Optional[int]:
        """Count active token sessions from Redis. Returns None if Redis is down (never a fabricated number or false zero)."""
        try:
            from core.redis_client import get_redis
            r = get_redis()
            if r is None:
                return None
            return sum(1 for _ in r.scan_iter(match="api_token:*", count=100))
        except Exception as exc:
            logger.debug("Could not query active sessions from Redis: %s", exc)
            return None


_GLOBAL_SERVICE: Optional[ObservatoryService] = None


def get_observatory_service() -> ObservatoryService:
    global _GLOBAL_SERVICE
    if _GLOBAL_SERVICE is None:
        _GLOBAL_SERVICE = ObservatoryService()
    return _GLOBAL_SERVICE
