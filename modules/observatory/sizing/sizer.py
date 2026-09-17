"""
Model capacity sizing engine.
Calculates memory budgets, KV-cache sizing, quantization requirements,
and deployment configuration tailored to host hardware and active inference engine.
Separates theoretical capacity estimation from executable deployment recommendations.
"""

from __future__ import annotations

import logging
import math
import shlex
from typing import Any, Dict, List, Optional
import psutil

from core.config import settings
from .catalog import get_model_spec
from .models import (
    DeploymentPlan,
    FitEvaluationResult,
    FitStatus,
    MemoryBudget,
    ModelSpec,
    PrecisionType,
    ValidatedDeploymentPlan,
)
from ..probes.cpu import CPUInspector
from ..probes.gpu import GPUInspector

logger = logging.getLogger("techyz.observatory.sizing")


class ModelSizer:
    """Evaluates host capacity against model architectures and target workloads."""

    def __init__(self):
        self._gpu_inspector = GPUInspector()
        self._cpu_inspector = CPUInspector()

    def evaluate(
        self,
        model_id: str,
        context_window: int = 8192,
        concurrency: int = 4,
        force_tp: Optional[int] = None,
    ) -> FitEvaluationResult:
        # 1. Input Validation
        if not model_id or not model_id.strip():
            raise ValueError("model_id is required.")

        clean_model_id = model_id.strip()
        spec = get_model_spec(clean_model_id)
        if not spec:
            raise ValueError(
                f"Model '{clean_model_id}' is not present in the model architecture catalog. "
                "Authoritative layer, hidden size, and head geometry are required for accurate sizing."
            )

        if context_window < 512:
            raise ValueError("context_window must be at least 512 tokens.")
        if context_window > spec.max_context_len:
            raise ValueError(
                f"Requested context_window ({context_window}) exceeds maximum supported "
                f"context length ({spec.max_context_len}) for model '{spec.display_name}'."
            )

        if concurrency < 1:
            raise ValueError("concurrency must be at least 1.")
        if concurrency > 256:
            raise ValueError("concurrency cannot exceed 256.")

        # 2. Inference Engine Validation (Must be ollama or vllm, reject unknown)
        active_engine = getattr(settings, "inference_engine", "ollama").strip().lower()
        if active_engine not in ("ollama", "vllm"):
            raise ValueError(
                f"Unsupported inference engine '{active_engine}'. Supported engines: 'ollama', 'vllm'."
            )

        # 3. Hardware Inspection
        gpu_summary = self._gpu_inspector.probe()
        total_gpus = gpu_summary.total_gpu_count
        gpus = gpu_summary.gpus

        if force_tp is not None:
            if force_tp < 1:
                raise ValueError("force_tp must be at least 1.")
            if total_gpus > 0 and force_tp > total_gpus:
                raise ValueError(
                    f"Requested tensor parallel size ({force_tp}) exceeds available GPU count ({total_gpus})."
                )
            if total_gpus == 0 and force_tp > 1:
                raise ValueError("force_tp > 1 is not supported in CPU-only mode.")
            if force_tp not in (1, 2, 4, 8, 16):
                raise ValueError(
                    f"Requested tensor parallel size ({force_tp}) is not a supported power-of-2 degree."
                )
            if spec.num_attention_heads % force_tp != 0:
                raise ValueError(
                    f"Requested tensor parallel size ({force_tp}) does not evenly divide model attention heads ({spec.num_attention_heads})."
                )

        total_vram_gb = sum(g.vram_total_gb for g in gpus)
        avail_vram_gb = sum(g.vram_available_gb for g in gpus)
        primary_gpu = gpus[0] if gpus else None
        primary_gpu_name = primary_gpu.model_name if primary_gpu else "CPU Fallback"
        is_unified_memory = any(getattr(g, "memory_type", "discrete") == "unified" for g in gpus)
        gpu_vendor = primary_gpu.vendor if primary_gpu else "Generic"

        # Theoretical weights calculations
        weights_fp16_gb = round(spec.parameter_count_b * 2.0, 2)
        weights_awq_gb = round(spec.parameter_count_b * 0.55, 2)
        weights_fp8_gb = round(spec.parameter_count_b * 1.05, 2)

        # Single sequence KV-cache byte calculation
        # Bytes per sequence = 2 * num_layers * num_kv_heads * head_dim * 2 (FP16) * context_window
        bytes_per_seq = 2 * spec.num_layers * spec.num_kv_heads * spec.head_dim * 2 * context_window
        seq_kv_gb = bytes_per_seq / (1024 ** 3)
        kv_cache_total_bytes = bytes_per_seq * max(concurrency, 1)
        kv_cache_gb = round(kv_cache_total_bytes / (1024 ** 3), 2)

        missing_prerequisites: List[str] = []

        # -------------------------------------------------------------------------
        # Branch A: CPU-Only Enterprise AI Workload Evaluation
        # -------------------------------------------------------------------------
        if total_gpus == 0:
            cpu_info = self._cpu_inspector.probe()
            vm = psutil.virtual_memory()
            host_ram_total_gb = round(vm.total / (1024 ** 3), 2)
            host_ram_avail_gb = round(vm.available / (1024 ** 3), 2)

            sys_overhead_gb = 2.0
            req_fp16 = weights_fp16_gb + kv_cache_gb + sys_overhead_gb
            req_q4 = weights_awq_gb + kv_cache_gb + sys_overhead_gb

            if cpu_info.amx_supported or cpu_info.avx512_supported:
                cpu_dtype = "bfloat16"
            elif cpu_info.avx2_supported:
                cpu_dtype = "float16"
            else:
                cpu_dtype = "float32"

            if host_ram_avail_gb >= req_fp16:
                status = FitStatus.FITS
                quant = "none"
                total_req = req_fp16
                used_weights = weights_fp16_gb
                advice = (
                    f"Model '{spec.display_name}' fits in Host RAM unquantized ({cpu_dtype}) "
                    f"with {round(host_ram_avail_gb - req_fp16, 1)} GB headroom."
                )
            elif host_ram_avail_gb >= req_q4:
                status = FitStatus.FITS_WITH_QUANT
                quant = "q4_k_m"
                total_req = req_q4
                used_weights = weights_awq_gb
                advice = (
                    f"Model '{spec.display_name}' fits in Host RAM using 4-bit quantization (Q4_K_M) "
                    f"with {round(host_ram_avail_gb - req_q4, 1)} GB headroom."
                )
            else:
                status = FitStatus.OOM
                quant = "q4_k_m"
                total_req = req_q4
                used_weights = weights_awq_gb
                shortfall = round(req_q4 - host_ram_avail_gb, 1)
                advice = (
                    f"Insufficient Host RAM for CPU execution: requires at least {total_req} GB (shortfall of {shortfall} GB). "
                    f"Available: {host_ram_avail_gb} GB. Reduce context window from {context_window} or upgrade host memory."
                )

            # Compute actual max_safe_concurrency
            if status == FitStatus.OOM:
                max_safe_concurrency = 0
            else:
                avail_for_kv = max(0.0, host_ram_avail_gb - (used_weights + sys_overhead_gb))
                max_safe_concurrency = max(0, int(avail_for_kv / seq_kv_gb)) if seq_kv_gb > 0 else 0
                max_safe_concurrency = min(max_safe_concurrency, 256)

            budget = MemoryBudget(
                weights_unquantized_gb=weights_fp16_gb,
                weights_quantized_gb=used_weights,
                kv_cache_gb=kv_cache_gb,
                runtime_overhead_gb=sys_overhead_gb,
                total_required_vram_gb=round(total_req, 2),
                headroom_vram_gb=round(max(0.0, host_ram_avail_gb - total_req), 2),
                cuda_overhead_gb=0.0,
                memory_type="host_ram",
            )

            # Validated deployment plan validation
            if status == FitStatus.OOM:
                missing_prerequisites.append(f"Model exceeds Host RAM capacity (shortfall of {shortfall} GB).")

            missing_prerequisites.append(
                "Runtime model presence and immutable artifact identity have not been verified on this host."
            )

            if active_engine == "vllm":
                # vLLM CPU mode policy: must have AVX-512 or AMX and registered artifact
                if not (cpu_info.avx512_supported or cpu_info.amx_supported):
                    missing_prerequisites.append("vLLM CPU mode requires an x86 host with AVX-512 or AMX extensions.")
                if quant != "none" and quant not in spec.registered_quantizations:
                    missing_prerequisites.append(f"No verified GGUF/CPU quantized artifact registered for '{spec.model_id}'.")

                target_id = spec.registered_quantizations.get(quant, spec.hf_repo_id)
                launch_args = ["vllm", "serve", target_id, "--device", "cpu", "--dtype", cpu_dtype]
                if quant != "none":
                    launch_args.extend(["--quantization", quant])
                launch_args.extend(["--max-model-len", str(context_window)])
                display_cmd = shlex.join(launch_args)
                launch_config = {
                    "engine": "vllm",
                    "model": target_id,
                    "device": "cpu",
                    "dtype": cpu_dtype,
                    "quantization": quant if quant != "none" else None,
                    "max_model_len": context_window,
                }
            else:
                # Ollama CPU native execution
                target_id = spec.ollama_model_id
                launch_args = ["ollama", "run", target_id]
                display_cmd = f"ollama run {shlex.quote(target_id)}"
                launch_config = {
                    "engine": "ollama",
                    "model": target_id,
                    "num_ctx": context_window,
                    "quantization": quant,
                }

            if missing_prerequisites:
                deployment_plan = None
            else:
                deployment_plan = ValidatedDeploymentPlan(
                    engine=active_engine,
                    engine_specific_model_id=target_id,
                    exact_model_revision=spec.exact_model_revision,
                    available_weight_format="gguf" if active_engine == "ollama" else "safetensors",
                    quantization_artifact=spec.registered_quantizations.get(quant) if quant != "none" else None,
                    gpu_count=0,
                    per_device_memory_gb=[host_ram_avail_gb],
                    topology="Host RAM (CPU)",
                    supported_dtype=cpu_dtype,
                    supported_tensor_parallel=1,
                    model_divisibility_constraints={"num_attention_heads": spec.num_attention_heads, "tensor_parallel": 1},
                    max_safe_concurrency=max_safe_concurrency,
                    deployment_command=display_cmd,
                    launch_arguments=launch_args,
                    launch_config=launch_config,
                )

            recommendation = DeploymentPlan(
                model_id=target_id,
                fit_status=status,
                recommended_quantization=quant,
                recommended_dtype=cpu_dtype,
                recommended_tensor_parallel=1,
                max_safe_concurrency=max_safe_concurrency,
                vllm_command_template=display_cmd if deployment_plan else "",
                scaling_advice=advice,
                launch_arguments=launch_args if deployment_plan else [],
                launch_config=launch_config if deployment_plan else {},
            )

            return FitEvaluationResult(
                model_id=spec.model_id,
                fit_status=status,
                hardware_evaluated={
                    "mode": "CPU-Only",
                    "total_gpus": 0,
                    "gpu_model": "CPU Mode (Host RAM)",
                    "host_ram_total_gb": host_ram_total_gb,
                    "host_ram_available_gb": host_ram_avail_gb,
                    "cpu_cores": cpu_info.physical_cores,
                    "amx_supported": cpu_info.amx_supported,
                    "avx512_supported": cpu_info.avx512_supported,
                    "amx": cpu_info.amx_supported,
                    "avx512": cpu_info.avx512_supported,
                    "recommended_dtype": cpu_dtype,
                    "overhead_type": "system",
                },
                memory_budget=budget,
                recommendation=recommendation,
                deployment_plan=deployment_plan,
                missing_prerequisites=missing_prerequisites,
                scaling_advice=advice,
                is_capacity_estimate_only=bool(missing_prerequisites),
            )

        # -------------------------------------------------------------------------
        # Branch B: GPU Hardware Accelerator Evaluation (NVIDIA, AMD ROCm, APU)
        # -------------------------------------------------------------------------
        # Tensor parallel selection: must be a supported power-of-2 that divides attention heads
        if force_tp is not None:
            tp = force_tp
        else:
            # Automatic TP: largest power of 2 <= total_gpus that divides num_attention_heads
            tp = 1
            for cand in (16, 8, 4, 2):
                if cand <= total_gpus and spec.num_attention_heads % cand == 0:
                    tp = cand
                    break

        participating_gpus = gpus[:tp] if total_gpus >= tp else gpus
        per_gpu_avail = [
            g.vram_available_gb if g.vram_available_gb > 0 else g.vram_total_gb
            for g in participating_gpus
        ]
        weakest_vram_gb = min(per_gpu_avail) if per_gpu_avail else 0.0

        if is_unified_memory and primary_gpu and getattr(primary_gpu, "estimated_usable_memory_gb", None) is not None:
            effective_vram = primary_gpu.estimated_usable_memory_gb
            weakest_vram_gb = effective_vram
        elif tp > 1:
            # Multi-GPU capacity is bounded by the weakest participating GPU
            effective_vram = weakest_vram_gb * tp
        else:
            effective_vram = avail_vram_gb if avail_vram_gb > 0 else total_vram_gb
            weakest_vram_gb = effective_vram

        if gpu_vendor == "NVIDIA":
            runtime_overhead_gb = 2.5 if total_gpus > 0 else 0.5
            overhead_type = "cuda"
            compute_cap_raw = primary_gpu.compute_capability if primary_gpu else "0.0"
            try:
                compute_cap = float(compute_cap_raw)
            except ValueError:
                compute_cap = 0.0
            dtype = "bfloat16" if compute_cap >= 8.0 else "float16"
            fp8_capable = (compute_cap >= 8.9)
        elif gpu_vendor == "AMD":
            runtime_overhead_gb = 2.0
            overhead_type = "rocm"
            dtype = "bfloat16"
            fp8_capable = False
        else:
            runtime_overhead_gb = 1.5
            overhead_type = "system" if is_unified_memory else "accelerator"
            dtype = "float16"
            fp8_capable = False

        req_fp16 = weights_fp16_gb + kv_cache_gb + runtime_overhead_gb
        req_fp8 = weights_fp8_gb + kv_cache_gb + runtime_overhead_gb
        req_awq = weights_awq_gb + kv_cache_gb + runtime_overhead_gb

        apu_note = (
            f" on Unified APU ({primary_gpu_name}) with {effective_vram} GB estimated usable memory "
            f"(host reserve: {getattr(primary_gpu, 'host_reserve_gb', 4.0)} GB)"
            if is_unified_memory else ""
        )

        # Check weakest GPU per-shard fit
        shard_overhead = runtime_overhead_gb / tp
        fits_fp16_weakest = (weakest_vram_gb >= ((weights_fp16_gb / tp) + (kv_cache_gb / tp) + shard_overhead))
        fits_fp8_weakest = (weakest_vram_gb >= ((weights_fp8_gb / tp) + (kv_cache_gb / tp) + shard_overhead))
        fits_awq_weakest = (weakest_vram_gb >= ((weights_awq_gb / tp) + (kv_cache_gb / tp) + shard_overhead))

        if fits_fp16_weakest and effective_vram >= req_fp16:
            status = FitStatus.FITS_WITH_TP if tp > 1 else FitStatus.FITS
            quant = "none"
            total_req = req_fp16
            used_weights = weights_fp16_gb
            tp_note = f" across {tp} GPUs" if tp > 1 else ""
            advice = (
                f"Model '{spec.display_name}' fits comfortably unquantized ({dtype}){apu_note}{tp_note} "
                f"with {round(effective_vram - req_fp16, 1)} GB headroom."
            )
        elif fp8_capable and fits_fp8_weakest and effective_vram >= req_fp8:
            status = FitStatus.FITS_WITH_QUANT
            quant = "fp8"
            total_req = req_fp8
            used_weights = weights_fp8_gb
            tp_note = f" across {tp} GPUs" if tp > 1 else ""
            advice = (
                f"Model '{spec.display_name}' fits using native FP8 quantization{apu_note}{tp_note} "
                f"with {round(effective_vram - req_fp8, 1)} GB headroom."
            )
        elif fits_awq_weakest and effective_vram >= req_awq:
            status = FitStatus.FITS_WITH_QUANT
            quant = "awq"
            total_req = req_awq
            used_weights = weights_awq_gb
            tp_note = f" across {tp} GPUs" if tp > 1 else ""
            advice = (
                f"Model '{spec.display_name}' fits using 4-bit AWQ quantization{apu_note}{tp_note} "
                f"with {round(effective_vram - req_awq, 1)} GB headroom for KV-cache."
            )
        else:
            status = FitStatus.OOM
            quant = "awq"
            total_req = req_awq
            used_weights = weights_awq_gb
            shortfall = round(req_awq - effective_vram, 1)
            mem_label = "Usable Memory" if is_unified_memory else "VRAM"
            advice = (
                f"Insufficient {mem_label}{apu_note}: requires at least {total_req} GB (shortfall of {shortfall} GB). "
                f"Available: {effective_vram} GB (weakest participating GPU: {weakest_vram_gb} GB). "
                f"Reduce context window from {context_window} or provision additional accelerator resources."
            )

        # Calculate max_safe_concurrency based on weakest participating GPU
        if status == FitStatus.OOM:
            max_safe_concurrency = 0
        else:
            per_gpu_seq_kv = seq_kv_gb / tp
            per_gpu_weights = used_weights / tp
            weakest_headroom_kv = max(0.0, weakest_vram_gb - (per_gpu_weights + shard_overhead))
            max_safe_concurrency = max(0, int(weakest_headroom_kv / per_gpu_seq_kv)) if per_gpu_seq_kv > 0 else 0
            max_safe_concurrency = min(max_safe_concurrency, 256)

        budget = MemoryBudget(
            weights_unquantized_gb=weights_fp16_gb,
            weights_quantized_gb=used_weights,
            kv_cache_gb=kv_cache_gb,
            runtime_overhead_gb=runtime_overhead_gb,
            total_required_vram_gb=round(total_req, 2),
            headroom_vram_gb=round(max(0.0, effective_vram - total_req), 2),
            cuda_overhead_gb=runtime_overhead_gb if gpu_vendor == "NVIDIA" else 0.0,
            memory_type="unified" if is_unified_memory else "vram",
        )

        # Validate prerequisite facts for Level 2 deployment plan
        if status == FitStatus.OOM:
            missing_prerequisites.append(f"Model exceeds accelerator memory (shortfall of {shortfall} GB).")

        if not spec.exact_model_revision or spec.exact_model_revision == "main":
            missing_prerequisites.append(
                "No immutable model revision or artifact digest is registered."
            )

        if quant != "none":
            registered_artifact = spec.registered_quantizations.get(quant)
            if not registered_artifact:
                missing_prerequisites.append(
                    f"No verified {quant.upper()} quantization artifact registered for '{spec.model_id}'."
                )
        else:
            registered_artifact = spec.hf_repo_id

        if spec.num_attention_heads % tp != 0:
            missing_prerequisites.append(
                f"Tensor parallel size {tp} does not evenly divide {spec.num_attention_heads} attention heads."
            )

        # Engine-specific commands & configurations
        if active_engine == "vllm":
            architecture = getattr(primary_gpu, "architecture", "unknown") if primary_gpu else "unknown"
            missing_prerequisites.append(
                "The selected vLLM image, accelerator runtime, driver, and model artifact "
                f"have not been live-validated together for {gpu_vendor} {architecture}."
            )
            target_id = registered_artifact if (quant != "none" and registered_artifact) else spec.hf_repo_id
            launch_args = ["vllm", "serve", target_id, "--dtype", dtype]
            if quant != "none":
                launch_args.extend(["--quantization", quant])
            if tp > 1:
                launch_args.extend(["--tensor-parallel-size", str(tp)])
            launch_args.extend(["--max-model-len", str(context_window)])
            launch_args.extend(["--gpu-memory-utilization", "0.90"])
            display_cmd = shlex.join(launch_args)
            launch_config = {
                "engine": "vllm",
                "model": target_id,
                "dtype": dtype,
                "quantization": quant if quant != "none" else None,
                "tensor_parallel_size": tp,
                "max_model_len": context_window,
                "gpu_memory_utilization": 0.90,
            }
        else:
            # Ollama: strictly engine-specific tag, NO vLLM flags, NO CUDA terminology
            target_id = spec.ollama_model_id
            launch_args = ["ollama", "run", target_id]
            display_cmd = f"ollama run {shlex.quote(target_id)}"
            launch_config = {
                "engine": "ollama",
                "model": target_id,
                "num_ctx": context_window,
                "quantization": quant,
            }

        if missing_prerequisites:
            deployment_plan = None
        else:
            deployment_plan = ValidatedDeploymentPlan(
                engine=active_engine,
                engine_specific_model_id=target_id,
                exact_model_revision=spec.exact_model_revision,
                available_weight_format="safetensors" if active_engine == "vllm" else "gguf",
                quantization_artifact=registered_artifact if quant != "none" else None,
                gpu_count=tp,
                per_device_memory_gb=per_gpu_avail,
                topology=(
                    f"AMD ROCm Accelerator ({'Unified APU' if is_unified_memory else 'Discrete VRAM'})"
                    if gpu_vendor == "AMD"
                    else (
                        f"NVIDIA CUDA Accelerator ({'Unified APU' if is_unified_memory else 'Discrete VRAM'})"
                        if gpu_vendor == "NVIDIA"
                        else f"{gpu_vendor} Accelerator ({'Unified APU' if is_unified_memory else 'Discrete VRAM'})"
                    )
                ),
                supported_dtype=dtype,
                supported_tensor_parallel=tp,
                model_divisibility_constraints={
                    "num_attention_heads": spec.num_attention_heads,
                    "num_kv_heads": spec.num_kv_heads,
                    "tensor_parallel": tp,
                    "heads_per_gpu": spec.num_attention_heads // tp,
                },
                max_safe_concurrency=max_safe_concurrency,
                deployment_command=display_cmd,
                launch_arguments=launch_args,
                launch_config=launch_config,
            )

        recommendation = DeploymentPlan(
            model_id=target_id,
            fit_status=status,
            recommended_quantization=quant,
            recommended_dtype=dtype,
            recommended_tensor_parallel=tp,
            max_safe_concurrency=max_safe_concurrency,
            vllm_command_template=display_cmd if deployment_plan else "",
            scaling_advice=advice,
            launch_arguments=launch_args if deployment_plan else [],
            launch_config=launch_config if deployment_plan else {},
        )

        return FitEvaluationResult(
            model_id=spec.model_id,
            fit_status=status,
            hardware_evaluated={
                "total_gpus": total_gpus,
                "participating_gpus": tp,
                "primary_gpu": primary_gpu_name,
                "gpu_model": primary_gpu_name,
                "total_vram_gb": total_vram_gb,
                "available_vram_gb": avail_vram_gb,
                "weakest_gpu_vram_gb": weakest_vram_gb,
                "vendor": gpu_vendor,
                "memory_type": "unified" if is_unified_memory else "discrete",
                "is_unified_memory": is_unified_memory,
                "estimated_usable_memory_gb": effective_vram if is_unified_memory else avail_vram_gb,
                "overhead_type": overhead_type,
            },
            memory_budget=budget,
            recommendation=recommendation,
            deployment_plan=deployment_plan,
            missing_prerequisites=missing_prerequisites,
            scaling_advice=advice,
            is_capacity_estimate_only=bool(missing_prerequisites),
        )
