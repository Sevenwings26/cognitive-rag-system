"""CPU hardware topology and instruction set probe."""

import os
import platform
import re
import psutil
from typing import List, Optional
from .base import BaseProbe, CPUInfo


class CPUInspector:
    """Inspects CPU hardware architecture, cores, and AI acceleration instruction sets (AVX-512, AMX, VNNI)."""

    def __init__(self, cache_static_probe: bool = True):
        self._cache_static_probe = cache_static_probe
        self._cached_info: Optional[CPUInfo] = None

    def probe(self, force_refresh: bool = False) -> CPUInfo:
        if not force_refresh and self._cached_info is not None:
            return self._cached_info

        arch = platform.machine()
        model_name = platform.processor() or "Unknown CPU"
        physical_cores = psutil.cpu_count(logical=False) or 1
        logical_threads = psutil.cpu_count(logical=True) or 1
        features: List[str] = []
        avx2 = False
        avx512 = False
        amx = False
        vnni = False

        if os.path.exists("/proc/cpuinfo"):
            try:
                with open("/proc/cpuinfo", "r", encoding="utf-8", errors="ignore") as f:
                    content = f.read()
                model_match = re.search(r"model name\s*:\s*(.*)", content)
                if model_match:
                    model_name = model_match.group(1).strip()
                flags_match = re.search(r"flags\s*:\s*(.*)", content)
                if flags_match:
                    features = flags_match.group(1).strip().split()
                    avx2 = "avx2" in features
                    avx512 = any(f.startswith("avx512") for f in features)
                    amx = any(f.startswith("amx") for f in features)
                    vnni = any("vnni" in f for f in features)
            except Exception:
                pass

        info = CPUInfo(
            architecture=arch,
            physical_cores=physical_cores,
            logical_threads=logical_threads,
            sockets=1,
            model_name=model_name,
            features=features,
            avx2_supported=avx2,
            avx512_supported=avx512,
            amx_supported=amx,
            vnni_supported=vnni,
        )

        if self._cache_static_probe:
            self._cached_info = info
        return info

    def clear_cache(self) -> None:
        """Clear cached CPU topology to force re-probing."""
        self._cached_info = None


class CPUProbe(BaseProbe):
    name = "cpu"

    def __init__(self):
        self._inspector = CPUInspector()

    def probe(self, force_refresh: bool = False) -> CPUInfo:
        return self._inspector.probe(force_refresh=force_refresh)
