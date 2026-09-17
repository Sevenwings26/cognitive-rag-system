"""Multi-tier GPU hardware accelerator inspector supporting NVIDIA and AMD/ROCm."""

import glob
import json
import logging
import os
import re
import shutil
import subprocess
from typing import Any, Dict, List, Optional, Tuple
import psutil

from .base import BaseProbe, GPUInfo, GPUSummary

logger = logging.getLogger("techyz.observatory.probes.gpu")

# Common ROCm SMI binary candidate paths
_ROCM_SMI_CANDIDATES = (
    "rocm-smi",
    "/opt/rocm/bin/rocm-smi",
    "/opt/rocm/opencl/bin/rocm-smi",
)

_AMD_SMI_CANDIDATES = (
    "amd-smi",
    "/opt/rocm/bin/amd-smi",
)

# Known AMD GPU architecture targets
_KNOWN_AMD_APU_ARCHS = {
    "gfx1150",  # Strix Point
    "gfx1151",  # Strix Halo
    "gfx1152",  # Strix Halo variant
    "gfx1103",  # Phoenix / Hawk Point
    "gfx1035",  # Rembrandt / Yellow Carp
    "gfx1036",  # Mendocino
    "gfx902",   # Raven / Picasso
    "gfx909",   # Renoir / Lucienne
    "gfx90c",   # Cezanne / Barcelo
}

_KNOWN_AMD_DISCRETE_ARCHS = {
    "gfx1100",  # Navi 31 (RX 7900 series)
    "gfx1101",  # Navi 32 (RX 7800 / 7700)
    "gfx1102",  # Navi 33 (RX 7600)
    "gfx1030",  # Navi 21 (RX 6900 / 6800)
    "gfx1031",  # Navi 22 (RX 6700)
    "gfx1032",  # Navi 23 (RX 6600)
    "gfx906",   # Vega 20 (Radeon VII, MI50, MI60)
    "gfx908",   # CDNA 1 (MI100)
    "gfx90a",   # CDNA 2 (MI210, MI250, MI250X)
    "gfx940",   # CDNA 3 (MI300A)
    "gfx942",   # CDNA 3 (MI300X)
}

_APU_NAME_KEYWORDS = (
    "8060s",
    "8060",
    "strix halo",
    "strix point",
    "strix",
    "ryzen ai",
    "radeon 890m",
    "radeon 880m",
    "radeon 780m",
    "radeon 680m",
    "phoenix",
    "hawk point",
    "apu",
)

_DISCRETE_NAME_KEYWORDS = (
    "rx 7",
    "rx 6",
    "rx 5",
    "radeon pro",
    "instinct",
    "navi 3",
    "navi 2",
    "mi300x",
    "mi250",
    "mi210",
    "mi100",
)


def _normalize_smi_dict(raw: Dict[str, Any]) -> Dict[str, Any]:
    """
    Normalize dictionary keys by lowercasing and stripping non-alphanumeric characters.
    Handles variations like 'Card Series', 'Card series', 'card_series', 'Device Name'.
    """
    norm: Dict[str, Any] = {}
    for k, v in raw.items():
        clean_key = re.sub(r"[^a-z0-9]", "", str(k).lower())
        norm[clean_key] = v
    return norm


def _is_amd_apu(arch: str, name_or_sku: str, vram_total_bytes: int) -> bool:
    """
    Accurately classify an AMD device as an APU (unified memory) vs. a Discrete GPU.
    Never classifies all Radeon devices as APUs (e.g. RX 7900 XTX is discrete).
    """
    arch_lower = (arch or "").lower().strip()
    name_lower = (name_or_sku or "").lower().strip()

    # 1. Authoritative architecture check
    if arch_lower in _KNOWN_AMD_APU_ARCHS:
        return True
    if arch_lower in _KNOWN_AMD_DISCRETE_ARCHS:
        return False

    # 2. Product name / SKU keywords
    for kw in _APU_NAME_KEYWORDS:
        if kw in name_lower:
            return True

    for kw in _DISCRETE_NAME_KEYWORDS:
        if kw in name_lower:
            return False

    # 3. Memory aperture threshold: small aperture (<= 1 GB) typically indicates APU BIOS carveout
    if 0 < vram_total_bytes <= 1 * (1024 ** 3):
        return True

    return False


def _resolve_host_reserve_gb(custom_reserve: Optional[float] = None) -> float:
    """Resolve the host RAM reserve in GB from settings or default."""
    if custom_reserve is not None and custom_reserve > 0:
        return float(custom_reserve)
    try:
        from core.config import settings
        return float(getattr(settings, "observatory_host_reserve_gb", 4.0))
    except Exception:
        return 4.0


class GPUInspector:
    """
    Inspects GPU hardware accelerators with a multi-tier fallback probe chain.
    Separates static hardware identity from dynamic memory metrics so dynamic
    host RAM and VRAM availability are never frozen in cache.
    """

    def __init__(
        self,
        cache_static_probes: bool = True,
        host_reserve_gb: Optional[float] = None,
    ):
        self._cache_static_probes = cache_static_probes
        self._host_reserve_gb = _resolve_host_reserve_gb(host_reserve_gb)
        self._cached_no_gpu: Optional[GPUSummary] = None
        self._cached_static_gpus: Optional[List[Dict[str, Any]]] = None
        self._cached_detection_method: str = "none"

    def probe(self, force_refresh: bool = False) -> GPUSummary:
        """
        Probe GPU hardware.
        When static caching is enabled:
        - GPU-less state is cached to avoid repeated failed subprocess invocations.
        - Detected GPUs cache static hardware identity (vendor, model, arch, memory_type),
          while dynamic memory metrics (available VRAM, host available RAM, estimated usable memory)
          are always refreshed dynamically.
        """
        # If previously confirmed no GPU and not forced, return cached GPU-less summary
        if not force_refresh and self._cached_no_gpu is not None:
            return self._cached_no_gpu

        # If static inventory is cached and not forced, refresh dynamic memory metrics
        if not force_refresh and self._cached_static_gpus is not None:
            refreshed = self._refresh_dynamic_memory(self._cached_static_gpus, self._cached_detection_method)
            if refreshed is not None:
                return refreshed

        # Tier 1: pynvml direct driver API (NVIDIA)
        gpus, method = self._probe_pynvml()
        if gpus:
            return self._finalize_and_cache(gpus, method)

        # Tier 2: nvidia-smi CLI (NVIDIA fallback)
        gpus, method = self._probe_nvidia_smi()
        if gpus:
            return self._finalize_and_cache(gpus, method)

        # Tier 3: rocm-smi CLI (AMD ROCm authoritative)
        gpus, method = self._probe_rocm_smi()
        if gpus:
            return self._finalize_and_cache(gpus, method)

        # Tier 4: amd-smi CLI (AMD newer tools)
        gpus, method = self._probe_amd_smi()
        if gpus:
            return self._finalize_and_cache(gpus, method)

        # Tier 5: AMD sysfs DRM & KFD topology inspection
        gpus, method = self._probe_amd_sysfs()
        if gpus:
            return self._finalize_and_cache(gpus, method)

        # Tier 6: lspci (partial inventory fallback)
        gpus, method = self._probe_lspci()
        if gpus:
            return self._finalize_and_cache(gpus, method)

        # No GPU detected
        summary = GPUSummary(gpu_available=False, total_gpu_count=0, gpus=[], detection_method="none")
        if self._cache_static_probes:
            self._cached_no_gpu = summary
        return summary

    def clear_cache(self) -> None:
        """Clear cached probe results to force complete re-detection on next probe."""
        self._cached_no_gpu = None
        self._cached_static_gpus = None
        self._cached_detection_method = "none"

    def _finalize_and_cache(self, gpus: List[GPUInfo], method: str) -> GPUSummary:
        summary = GPUSummary(gpu_available=True, total_gpu_count=len(gpus), gpus=gpus, detection_method=method)
        if self._cache_static_probes:
            self._cached_static_gpus = [
                {
                    "slot": g.slot,
                    "vendor": g.vendor,
                    "model_name": g.model_name,
                    "compute_capability": g.compute_capability,
                    "architecture": g.architecture,
                    "driver_version": g.driver_version,
                    "uuid": g.uuid,
                    "memory_type": g.memory_type,
                    "discrete_vram_total_gb": g.discrete_vram_total_gb,
                    "unified_memory_pool_gb": g.unified_memory_pool_gb,
                    "detection_confidence": g.detection_confidence,
                    "host_reserve_gb": g.host_reserve_gb,
                }
                for g in gpus
            ]
            self._cached_detection_method = method
        return summary

    def _refresh_dynamic_memory(
        self,
        static_inventory: List[Dict[str, Any]],
        method: str,
    ) -> Optional[GPUSummary]:
        """Dynamically refresh available memory for cached static GPU inventory."""
        vm = psutil.virtual_memory()
        host_avail_gb = round(vm.available / (1024 ** 3), 2)
        refreshed_gpus: List[GPUInfo] = []

        # Refresh NVIDIA dynamic memory via NVML if possible
        nvidia_dynamic: Dict[int, float] = {}
        nvidia_dynamic_bytes: Dict[int, Tuple[int, int]] = {}
        nvidia_compute: Dict[int, Optional[float]] = {}
        if method == "pynvml":
            try:
                import pynvml
                pynvml.nvmlInit()
                count = pynvml.nvmlDeviceGetCount()
                for i in range(count):
                    handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                    mem = pynvml.nvmlDeviceGetMemoryInfo(handle)
                    nvidia_dynamic[i] = round(mem.free / (1024 ** 3), 2)
                    nvidia_dynamic_bytes[i] = (int(mem.total), int(mem.used))
                    try:
                        rates = pynvml.nvmlDeviceGetUtilizationRates(handle)
                        nvidia_compute[i] = float(rates.gpu)
                    except Exception:
                        nvidia_compute[i] = None
                pynvml.nvmlShutdown()
            except Exception:
                pass

        for idx, item in enumerate(static_inventory):
            mem_type = item["memory_type"]
            reserve = item.get("host_reserve_gb", self._host_reserve_gb)

            if mem_type == "unified":
                # APU: Always re-evaluate available memory from dynamic host RAM
                pool = item["unified_memory_pool_gb"] or round(vm.total / (1024 ** 3), 2)
                usable = max(0.0, round(min(host_avail_gb, pool) - reserve, 2))
                refreshed_gpus.append(GPUInfo(
                    slot=item["slot"],
                    vendor=item["vendor"],
                    model_name=item["model_name"],
                    vram_total_gb=0.0,
                    vram_available_gb=0.0,
                    compute_capability=item["compute_capability"],
                    driver_version=item["driver_version"],
                    uuid=item["uuid"],
                    memory_type="unified",
                    architecture=item["architecture"],
                    discrete_vram_total_gb=0.0,
                    discrete_vram_available_gb=0.0,
                    unified_memory_pool_gb=pool,
                    host_ram_available_gb=host_avail_gb,
                    estimated_usable_memory_gb=usable,
                    detection_confidence=item["detection_confidence"],
                    host_reserve_gb=reserve,
                ))
            elif mem_type == "discrete":
                d_total = item["discrete_vram_total_gb"]
                d_avail = nvidia_dynamic.get(idx, item.get("discrete_vram_available_gb", d_total))
                if idx in nvidia_dynamic_bytes:
                    t_bytes, u_bytes = nvidia_dynamic_bytes[idx]
                else:
                    t_bytes = int(d_total * (1024 ** 3)) if d_total > 0 else 0
                    a_bytes = int(d_avail * (1024 ** 3)) if d_avail > 0 else 0
                    u_bytes = max(0, t_bytes - a_bytes)
                mem_util = round((u_bytes / t_bytes * 100.0), 1) if t_bytes > 0 else None
                comp_util = nvidia_compute.get(idx, None)

                refreshed_gpus.append(GPUInfo(
                    slot=item["slot"],
                    vendor=item["vendor"],
                    model_name=item["model_name"],
                    vram_total_gb=d_total,
                    vram_available_gb=d_avail,
                    compute_capability=item["compute_capability"],
                    driver_version=item["driver_version"],
                    uuid=item["uuid"],
                    memory_type="discrete",
                    architecture=item["architecture"],
                    discrete_vram_total_gb=d_total,
                    discrete_vram_available_gb=d_avail,
                    unified_memory_pool_gb=None,
                    host_ram_available_gb=host_avail_gb,
                    estimated_usable_memory_gb=d_avail,
                    detection_confidence=item["detection_confidence"],
                    host_reserve_gb=reserve,
                    gpu_memory_used_bytes=u_bytes,
                    gpu_memory_total_bytes=t_bytes,
                    gpu_memory_utilization_percent=mem_util,
                    gpu_compute_utilization_percent=comp_util,
                ))
            else:
                # Unknown / partial fallback
                refreshed_gpus.append(GPUInfo(
                    slot=item["slot"],
                    vendor=item["vendor"],
                    model_name=item["model_name"],
                    vram_total_gb=0.0,
                    vram_available_gb=0.0,
                    compute_capability=item["compute_capability"],
                    driver_version=item["driver_version"],
                    uuid=item["uuid"],
                    memory_type="unknown",
                    architecture=item["architecture"],
                    discrete_vram_total_gb=0.0,
                    discrete_vram_available_gb=0.0,
                    unified_memory_pool_gb=None,
                    host_ram_available_gb=host_avail_gb,
                    estimated_usable_memory_gb=0.0,
                    detection_confidence=item["detection_confidence"],
                    host_reserve_gb=reserve,
                ))

        return GPUSummary(
            gpu_available=True,
            total_gpu_count=len(refreshed_gpus),
            gpus=refreshed_gpus,
            detection_method=method,
        )

    # -------------------------------------------------------------------------
    # Tier 1: NVIDIA NVML
    # -------------------------------------------------------------------------
    def _probe_pynvml(self) -> Tuple[List[GPUInfo], str]:
        try:
            import pynvml
            pynvml.nvmlInit()
            count = pynvml.nvmlDeviceGetCount()
            gpus = []
            driver_ver = pynvml.nvmlSystemGetDriverVersion()
            if isinstance(driver_ver, bytes):
                driver_ver = driver_ver.decode("utf-8")

            vm = psutil.virtual_memory()
            host_avail_gb = round(vm.available / (1024 ** 3), 2)

            for i in range(count):
                handle = pynvml.nvmlDeviceGetHandleByIndex(i)
                name = pynvml.nvmlDeviceGetName(handle)
                if isinstance(name, bytes):
                    name = name.decode("utf-8")
                pci_info = pynvml.nvmlDeviceGetPciInfo(handle)
                bus_id = pci_info.busId
                if isinstance(bus_id, bytes):
                    bus_id = bus_id.decode("utf-8")

                mem_info = pynvml.nvmlDeviceGetMemoryInfo(handle)
                total_gb = round(mem_info.total / (1024 ** 3), 2)
                free_gb = round(mem_info.free / (1024 ** 3), 2)
                cap = pynvml.nvmlDeviceGetCudaComputeCapability(handle)
                cc = f"{cap[0]}.{cap[1]}"

                t_bytes = int(mem_info.total)
                u_bytes = int(mem_info.used)
                mem_util = round((u_bytes / t_bytes * 100.0), 1) if t_bytes > 0 else None

                comp_util = None
                try:
                    util_rates = pynvml.nvmlDeviceGetUtilizationRates(handle)
                    comp_util = float(util_rates.gpu)
                except Exception:
                    comp_util = None

                try:
                    uuid_val = pynvml.nvmlDeviceGetUUID(handle)
                    if isinstance(uuid_val, bytes):
                        uuid_val = uuid_val.decode("utf-8")
                except Exception:
                    uuid_val = None

                gpus.append(GPUInfo(
                    slot=bus_id,
                    vendor="NVIDIA",
                    model_name=str(name),
                    vram_total_gb=total_gb,
                    vram_available_gb=free_gb,
                    compute_capability=cc,
                    driver_version=str(driver_ver),
                    uuid=uuid_val,
                    memory_type="discrete",
                    architecture=f"sm_{cap[0]}{cap[1]}",
                    discrete_vram_total_gb=total_gb,
                    discrete_vram_available_gb=free_gb,
                    unified_memory_pool_gb=None,
                    host_ram_available_gb=host_avail_gb,
                    estimated_usable_memory_gb=free_gb,
                    detection_confidence="authoritative",
                    host_reserve_gb=self._host_reserve_gb,
                    gpu_memory_used_bytes=u_bytes,
                    gpu_memory_total_bytes=t_bytes,
                    gpu_memory_utilization_percent=mem_util,
                    gpu_compute_utilization_percent=comp_util,
                ))
            pynvml.nvmlShutdown()
            return gpus, "pynvml"
        except Exception as exc:
            logger.debug("pynvml probe skipped/failed: %s", exc)
            return [], "none"

    # -------------------------------------------------------------------------
    # Tier 2: NVIDIA SMI CLI
    # -------------------------------------------------------------------------
    def _probe_nvidia_smi(self) -> Tuple[List[GPUInfo], str]:
        if not shutil.which("nvidia-smi"):
            return [], "none"

        fields = "pci.bus_id,name,memory.total,memory.free,driver_version,gpu_uuid,utilization.gpu"
        try:
            res = subprocess.run(
                ["nvidia-smi", f"--query-gpu={fields}", "--format=csv,noheader,nounits"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode != 0 or not res.stdout.strip():
                return [], "none"

            vm = psutil.virtual_memory()
            host_avail_gb = round(vm.available / (1024 ** 3), 2)
            gpus = []

            for line in res.stdout.strip().splitlines():
                parts = [p.strip() for p in line.split(",")]
                if len(parts) >= 6:
                    bus_id, name, total_mb, free_mb, driver_ver, uuid = parts[:6]
                    try:
                        total_gb = round(float(total_mb) / 1024.0, 2)
                        free_gb = round(float(free_mb) / 1024.0, 2)
                        t_bytes = int(float(total_mb) * 1024 * 1024)
                        a_bytes = int(float(free_mb) * 1024 * 1024)
                        u_bytes = max(0, t_bytes - a_bytes)
                    except ValueError:
                        total_gb, free_gb = 0.0, 0.0
                        t_bytes, u_bytes = 0, 0

                    mem_util = round((u_bytes / t_bytes * 100.0), 1) if t_bytes > 0 else None
                    comp_util = None
                    if len(parts) >= 7:
                        try:
                            comp_util = float(parts[6].replace("%", "").strip())
                        except ValueError:
                            comp_util = None

                    gpus.append(GPUInfo(
                        slot=bus_id,
                        vendor="NVIDIA",
                        model_name=name,
                        vram_total_gb=total_gb,
                        vram_available_gb=free_gb,
                        compute_capability="unknown",
                        driver_version=driver_ver,
                        uuid=uuid,
                        memory_type="discrete",
                        architecture="nvidia-unknown",
                        discrete_vram_total_gb=total_gb,
                        discrete_vram_available_gb=free_gb,
                        unified_memory_pool_gb=None,
                        host_ram_available_gb=host_avail_gb,
                        estimated_usable_memory_gb=free_gb,
                        detection_confidence="authoritative",
                        host_reserve_gb=self._host_reserve_gb,
                        gpu_memory_used_bytes=u_bytes,
                        gpu_memory_total_bytes=t_bytes,
                        gpu_memory_utilization_percent=mem_util,
                        gpu_compute_utilization_percent=comp_util,
                    ))
            return gpus, "nvidia-smi"
        except Exception as exc:
            logger.debug("nvidia-smi probe failed: %s", exc)
            return [], "none"

    # -------------------------------------------------------------------------
    # Tier 3: AMD ROCm SMI CLI (Authoritative AMD probe)
    # -------------------------------------------------------------------------
    def _probe_rocm_smi(self) -> Tuple[List[GPUInfo], str]:
        binary = None
        for cand in _ROCM_SMI_CANDIDATES:
            if shutil.which(cand) or os.path.exists(cand):
                binary = cand
                break
        if not binary:
            return [], "none"

        try:
            res = subprocess.run(
                [binary, "--showid", "--showproductname", "--showmeminfo", "vram", "--json"],
                capture_output=True,
                text=True,
                timeout=5,
            )
            if res.returncode != 0 or not res.stdout.strip():
                return [], "none"

            data = json.loads(res.stdout.strip())
            gpus = []
            vm = psutil.virtual_memory()
            host_ram_total_gb = round(vm.total / (1024 ** 3), 2)
            host_avail_gb = round(vm.available / (1024 ** 3), 2)

            for key, info in data.items():
                if not key.startswith("card"):
                    continue

                norm = _normalize_smi_dict(info)

                # Resolve device architecture
                arch = (
                    norm.get("gfxversion")
                    or norm.get("targetgraphicsip")
                    or "amd-unknown"
                )

                # Resolve model name with strict priority:
                # Device Name > Card Series > Card SKU > Card Model
                dev_name = norm.get("devicename")
                card_series = norm.get("cardseries")
                card_sku = norm.get("cardsku")
                card_model = norm.get("cardmodel")

                prod_name = ""
                if dev_name and not dev_name.lower().startswith("device"):
                    prod_name = str(dev_name).strip()
                elif card_series and not card_series.lower().startswith("0x"):
                    prod_name = str(card_series).strip()
                elif card_sku:
                    prod_name = f"AMD {card_sku}".strip()
                elif card_model and not card_model.lower().startswith("0x"):
                    prod_name = str(card_model).strip()

                # If architecture is known APU (e.g. gfx1151 Strix Halo), refine generic names
                if arch == "gfx1151":
                    if not prod_name or prod_name in ("AMD Radeon Graphics", "AMD Radeon / ROCm Accelerator"):
                        prod_name = "AMD Radeon 8060S Graphics"
                    elif "8060" not in prod_name and "strix" not in prod_name.lower():
                        prod_name = f"{prod_name} (Radeon 8060S)"
                elif not prod_name:
                    prod_name = "AMD Radeon / ROCm Accelerator"

                # Parse memory fields
                vram_total_bytes = 0
                vram_used_bytes = 0
                for tk in ("vramtotalmemoryb", "vramtotalb", "vramtotal"):
                    if tk in norm:
                        try:
                            vram_total_bytes = int(float(norm[tk]))
                            break
                        except (ValueError, TypeError):
                            pass

                for uk in ("vramtotalusedmemoryb", "vramusedb", "vramused"):
                    if uk in norm:
                        try:
                            vram_used_bytes = int(float(norm[uk]))
                            break
                        except (ValueError, TypeError):
                            pass

                # Authoritative APU vs Discrete determination
                sku_for_check = f"{prod_name} {card_sku or ''} {card_series or ''}"
                is_apu = _is_amd_apu(arch, sku_for_check, vram_total_bytes)

                if is_apu:
                    # AMD APU: Unified memory architecture
                    # Do NOT label shared host memory as discrete VRAM!
                    mem_type = "unified"
                    discrete_total = 0.0
                    discrete_avail = 0.0
                    vram_total_gb = 0.0
                    vram_available_gb = 0.0

                    reported_vram_gb = round(vram_total_bytes / (1024 ** 3), 2)
                    unified_pool = reported_vram_gb if reported_vram_gb > 1.0 else host_ram_total_gb
                    usable_memory = max(0.0, round(min(host_avail_gb, unified_pool) - self._host_reserve_gb, 2))
                else:
                    # AMD Discrete GPU (e.g. RX 7900 XTX, Instinct MI300X)
                    mem_type = "discrete"
                    total_gb = round(vram_total_bytes / (1024 ** 3), 2) if vram_total_bytes > 0 else 0.0
                    used_gb = round(vram_used_bytes / (1024 ** 3), 2) if vram_used_bytes > 0 else 0.0
                    free_gb = max(0.0, round(total_gb - used_gb, 2))

                    discrete_total = total_gb
                    discrete_avail = free_gb
                    vram_total_gb = total_gb
                    vram_available_gb = free_gb
                    unified_pool = None
                    usable_memory = free_gb

                dev_id = norm.get("deviceid")
                uuid_val = f"amd-{dev_id}" if dev_id else None

                mem_util = round((vram_used_bytes / vram_total_bytes * 100.0), 1) if vram_total_bytes > 0 else None
                comp_util = None
                raw_gpu_use = norm.get("gpuuse") or norm.get("gpu_use") or norm.get("gpu_busy_percent")
                if raw_gpu_use is not None:
                    try:
                        comp_util = float(str(raw_gpu_use).replace("%", "").strip())
                    except ValueError:
                        comp_util = None

                gpus.append(GPUInfo(
                    slot=key,
                    vendor="AMD",
                    model_name=prod_name,
                    vram_total_gb=vram_total_gb,
                    vram_available_gb=vram_available_gb,
                    compute_capability=str(arch),
                    driver_version="rocm",
                    uuid=uuid_val,
                    memory_type=mem_type,
                    architecture=str(arch),
                    discrete_vram_total_gb=discrete_total,
                    discrete_vram_available_gb=discrete_avail,
                    unified_memory_pool_gb=unified_pool,
                    host_ram_available_gb=host_avail_gb,
                    estimated_usable_memory_gb=usable_memory,
                    detection_confidence="authoritative",
                    host_reserve_gb=self._host_reserve_gb,
                    gpu_memory_used_bytes=vram_used_bytes,
                    gpu_memory_total_bytes=vram_total_bytes,
                    gpu_memory_utilization_percent=mem_util,
                    gpu_compute_utilization_percent=comp_util,
                ))

            return gpus, "rocm-smi" if gpus else "none"
        except (subprocess.TimeoutExpired, json.JSONDecodeError, Exception) as exc:
            logger.debug("rocm-smi probe failed or timed out: %s", exc)
            return [], "none"

    # -------------------------------------------------------------------------
    # Tier 4: amd-smi CLI
    # -------------------------------------------------------------------------
    def _probe_amd_smi(self) -> Tuple[List[GPUInfo], str]:
        binary = None
        for cand in _AMD_SMI_CANDIDATES:
            if shutil.which(cand) or os.path.exists(cand):
                binary = cand
                break
        if not binary:
            return [], "none"

        try:
            res = subprocess.run([binary, "static", "--json"], capture_output=True, text=True, timeout=5)
            if res.returncode != 0 or not res.stdout.strip():
                return [], "none"

            data = json.loads(res.stdout.strip())
            gpus = []
            vm = psutil.virtual_memory()
            host_ram_total_gb = round(vm.total / (1024 ** 3), 2)
            host_avail_gb = round(vm.available / (1024 ** 3), 2)

            device_list = data if isinstance(data, list) else data.get("devices", [])
            for idx, dev in enumerate(device_list):
                norm = _normalize_smi_dict(dev)
                name = norm.get("marketname") or norm.get("devicename") or "AMD Accelerator"
                vram_info = dev.get("vram", {})
                vram_total_bytes = vram_info.get("size", 0) or vram_info.get("total", 0)
                arch = dev.get("asic", {}).get("target_graphics_ip", "amd-unknown")

                is_apu = _is_amd_apu(arch, name, vram_total_bytes)
                if is_apu:
                    mem_type = "unified"
                    discrete_total = 0.0
                    discrete_avail = 0.0
                    vram_total_gb = 0.0
                    vram_available_gb = 0.0
                    reported_vram_gb = round(vram_total_bytes / (1024 ** 3), 2)
                    unified_pool = reported_vram_gb if reported_vram_gb > 1.0 else host_ram_total_gb
                    usable_memory = max(0.0, round(min(host_avail_gb, unified_pool) - self._host_reserve_gb, 2))
                else:
                    mem_type = "discrete"
                    total_gb = round(vram_total_bytes / (1024 ** 3), 2)
                    discrete_total = total_gb
                    discrete_avail = total_gb
                    vram_total_gb = total_gb
                    vram_available_gb = total_gb
                    unified_pool = None
                    usable_memory = total_gb

                gpus.append(GPUInfo(
                    slot=f"gpu:{idx}",
                    vendor="AMD",
                    model_name=str(name),
                    vram_total_gb=vram_total_gb,
                    vram_available_gb=vram_available_gb,
                    compute_capability=str(arch),
                    driver_version="amd-smi",
                    memory_type=mem_type,
                    architecture=str(arch),
                    discrete_vram_total_gb=discrete_total,
                    discrete_vram_available_gb=discrete_avail,
                    unified_memory_pool_gb=unified_pool,
                    host_ram_available_gb=host_avail_gb,
                    estimated_usable_memory_gb=usable_memory,
                    detection_confidence="authoritative",
                    host_reserve_gb=self._host_reserve_gb,
                ))
            return gpus, "amd-smi" if gpus else "none"
        except Exception as exc:
            logger.debug("amd-smi probe failed: %s", exc)
            return [], "none"

    # -------------------------------------------------------------------------
    # Tier 5: AMD sysfs DRM & KFD topology inspection
    # -------------------------------------------------------------------------
    def _probe_amd_sysfs(self) -> Tuple[List[GPUInfo], str]:
        drm_cards = glob.glob("/sys/class/drm/card[0-9]*/device")
        gpus: List[GPUInfo] = []
        vm = psutil.virtual_memory()
        host_ram_total_gb = round(vm.total / (1024 ** 3), 2)
        host_avail_gb = round(vm.available / (1024 ** 3), 2)

        # Parse KFD topology nodes mapped per render minor and device ID
        # Avoids assigning the first discovered GFX arch to every card
        kfd_arch_by_minor: Dict[str, str] = {}
        kfd_arch_by_device: Dict[str, str] = {}
        kfd_props = glob.glob("/sys/devices/virtual/kfd/kfd/topology/nodes/*/properties")
        for kp in kfd_props:
            try:
                node_arch = ""
                drm_render_minor = ""
                dev_id = ""
                with open(kp, "r") as kf:
                    for line in kf:
                        line_s = line.strip()
                        if line_s.startswith("name gfx"):
                            node_arch = line_s.split()[-1].strip()
                        elif line_s.startswith("drm_render_minor"):
                            drm_render_minor = line_s.split()[-1].strip()
                        elif line_s.startswith("device_id"):
                            dev_id = line_s.split()[-1].strip().lower()
                if node_arch:
                    if drm_render_minor:
                        kfd_arch_by_minor[drm_render_minor] = node_arch
                    if dev_id:
                        kfd_arch_by_device[dev_id] = node_arch
            except Exception:
                pass

        for card_path in drm_cards:
            vendor_file = os.path.join(card_path, "vendor")
            if not os.path.exists(vendor_file):
                continue
            try:
                with open(vendor_file, "r") as f:
                    vendor_id = f.read().strip().lower()
                # AMD PCI Vendor ID is 0x1002
                if vendor_id != "0x1002":
                    continue

                card_name = os.path.basename(os.path.dirname(card_path))
                model_name = "AMD Radeon Graphics"
                product_file = os.path.join(card_path, "product_name")
                if os.path.exists(product_file):
                    with open(product_file, "r") as pf:
                        model_name = pf.read().strip() or model_name

                # Device ID from sysfs
                dev_file = os.path.join(card_path, "device")
                pci_dev_id = ""
                if os.path.exists(dev_file):
                    with open(dev_file, "r") as df:
                        pci_dev_id = df.read().strip().lower()

                # Associate KFD topology node to THIS specific DRM card
                # Check render minor link under card
                arch = "amd-unknown"
                card_parent = os.path.dirname(card_path)
                render_nodes = glob.glob(os.path.join(card_parent, "renderD*"))
                for rn in render_nodes:
                    rn_basename = os.path.basename(rn)
                    minor_num = rn_basename.replace("renderD", "")
                    if minor_num in kfd_arch_by_minor:
                        arch = kfd_arch_by_minor[minor_num]
                        break

                if arch == "amd-unknown" and pci_dev_id:
                    clean_id = pci_dev_id.replace("0x", "")
                    arch = kfd_arch_by_device.get(pci_dev_id) or kfd_arch_by_device.get(clean_id) or "amd-unknown"

                # Total & used VRAM from sysfs
                vram_total_file = os.path.join(card_path, "mem_info_vram_total")
                vram_used_file = os.path.join(card_path, "mem_info_vram_used")
                total_bytes = 0
                used_bytes = 0
                if os.path.exists(vram_total_file):
                    with open(vram_total_file, "r") as vf:
                        total_bytes = int(vf.read().strip() or 0)
                if os.path.exists(vram_used_file):
                    with open(vram_used_file, "r") as uf:
                        used_bytes = int(uf.read().strip() or 0)

                is_apu = _is_amd_apu(arch, model_name, total_bytes)

                if is_apu:
                    mem_type = "unified"
                    discrete_total = 0.0
                    discrete_avail = 0.0
                    vram_total_gb = 0.0
                    vram_available_gb = 0.0
                    reported_vram_gb = round(total_bytes / (1024 ** 3), 2)
                    unified_pool = reported_vram_gb if reported_vram_gb > 1.0 else host_ram_total_gb
                    usable_memory = max(0.0, round(min(host_avail_gb, unified_pool) - self._host_reserve_gb, 2))
                    if arch == "gfx1151" and model_name == "AMD Radeon Graphics":
                        model_name = "Radeon 8060S Graphics"
                    confidence = "authoritative"
                else:
                    mem_type = "discrete"
                    total_gb = round(total_bytes / (1024 ** 3), 2)
                    free_gb = max(0.0, round((total_bytes - used_bytes) / (1024 ** 3), 2))
                    discrete_total = total_gb
                    discrete_avail = free_gb
                    vram_total_gb = total_gb
                    vram_available_gb = free_gb
                    unified_pool = None
                    usable_memory = free_gb
                    confidence = "authoritative"

                sysfs_mem_util = round((used_bytes / total_bytes * 100.0), 1) if total_bytes > 0 else None
                sysfs_comp_util = None
                for busy_candidate in [
                    os.path.join(card_path, "device", "gpu_busy_percent"),
                    os.path.join(card_path, "gpu_busy_percent"),
                ]:
                    if os.path.exists(busy_candidate):
                        try:
                            with open(busy_candidate, "r") as bf:
                                sysfs_comp_util = float(bf.read().strip())
                                break
                        except Exception:
                            pass

                gpus.append(GPUInfo(
                    slot=card_name,
                    vendor="AMD",
                    model_name=model_name,
                    vram_total_gb=vram_total_gb,
                    vram_available_gb=vram_available_gb,
                    compute_capability=arch,
                    driver_version="amdgpu-sysfs",
                    memory_type=mem_type,
                    architecture=arch,
                    discrete_vram_total_gb=discrete_total,
                    discrete_vram_available_gb=discrete_avail,
                    unified_memory_pool_gb=unified_pool,
                    host_ram_available_gb=host_avail_gb,
                    estimated_usable_memory_gb=usable_memory,
                    detection_confidence=confidence,
                    host_reserve_gb=self._host_reserve_gb,
                    gpu_memory_used_bytes=used_bytes,
                    gpu_memory_total_bytes=total_bytes,
                    gpu_memory_utilization_percent=sysfs_mem_util,
                    gpu_compute_utilization_percent=sysfs_comp_util,
                ))
            except Exception as e:
                logger.debug("sysfs parsing error on %s: %s", card_path, e)

        # Fallback: /dev/kfd exists without authoritative inventory
        # Return partial inventory with memory_type="unknown" and unavailable capacity
        # Never manufacture host RAM - 4GB as usable GPU VRAM
        if not gpus and os.path.exists("/dev/kfd"):
            arch = "amd-kfd"
            for kp in kfd_props:
                try:
                    with open(kp, "r") as kf:
                        for line in kf:
                            if line.startswith("name gfx"):
                                arch = line.split()[-1].strip()
                                break
                except Exception:
                    pass

            gpus.append(GPUInfo(
                slot="kfd:0",
                vendor="AMD",
                model_name="AMD ROCm / KFD Compute Node",
                vram_total_gb=0.0,
                vram_available_gb=0.0,
                compute_capability=arch,
                driver_version="kfd",
                memory_type="unknown",
                architecture=arch,
                discrete_vram_total_gb=0.0,
                discrete_vram_available_gb=0.0,
                unified_memory_pool_gb=None,
                host_ram_available_gb=host_avail_gb,
                estimated_usable_memory_gb=0.0,
                detection_confidence="partial",
                host_reserve_gb=self._host_reserve_gb,
            ))

        return gpus, "amd-sysfs" if gpus else "none"

    # -------------------------------------------------------------------------
    # Tier 6: lspci (Partial inventory fallback)
    # -------------------------------------------------------------------------
    def _probe_lspci(self) -> Tuple[List[GPUInfo], str]:
        if not shutil.which("lspci"):
            return [], "none"
        try:
            res = subprocess.run(["lspci"], capture_output=True, text=True, timeout=5)
            if res.returncode != 0:
                return [], "none"
            gpus = []
            vm = psutil.virtual_memory()
            host_avail_gb = round(vm.available / (1024 ** 3), 2)

            for line in res.stdout.strip().splitlines():
                lower = line.lower()
                if "vga compatible controller" in lower or "3d controller" in lower:
                    vendor = "NVIDIA" if "nvidia" in lower else ("AMD" if "amd" in lower or "ati" in lower else "Generic")
                    slot = line.split()[0] if line.split() else "Unknown"
                    desc = line.split(":", 2)[-1].strip() if ":" in line else line

                    # lspci cannot authoritatively determine memory capacity
                    # Return partial inventory with memory_type="unknown"
                    gpus.append(GPUInfo(
                        slot=slot,
                        vendor=vendor,
                        model_name=desc,
                        vram_total_gb=0.0,
                        vram_available_gb=0.0,
                        compute_capability="unknown",
                        driver_version="unknown",
                        memory_type="unknown",
                        architecture="unknown",
                        discrete_vram_total_gb=0.0,
                        discrete_vram_available_gb=0.0,
                        unified_memory_pool_gb=None,
                        host_ram_available_gb=host_avail_gb,
                        estimated_usable_memory_gb=0.0,
                        detection_confidence="partial",
                        host_reserve_gb=self._host_reserve_gb,
                    ))
            return gpus, "lspci" if gpus else "none"
        except Exception:
            return [], "none"


class GPUProbe(BaseProbe):
    name = "gpu"

    def __init__(self):
        self._inspector = GPUInspector()

    def probe(self) -> GPUSummary:
        return self._inspector.probe()
