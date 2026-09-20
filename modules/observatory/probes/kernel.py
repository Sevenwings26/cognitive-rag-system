"""Kernel settings and resource limits probe."""

import os
import resource
from .base import BaseProbe, KernelInfo


class KernelInspector:
    def probe(self) -> KernelInfo:
        max_map = 65530
        if os.path.exists("/proc/sys/vm/max_map_count"):
            try:
                with open("/proc/sys/vm/max_map_count", "r", encoding="utf-8") as f:
                    max_map = int(f.read().strip())
            except Exception:
                pass

        nofile_soft = 1024
        try:
            soft, _ = resource.getrlimit(resource.RLIMIT_NOFILE)
            nofile_soft = soft
        except Exception:
            pass

        thp = "unknown"
        if os.path.exists("/sys/kernel/mm/transparent_hugepage/enabled"):
            try:
                with open("/sys/kernel/mm/transparent_hugepage/enabled", "r", encoding="utf-8") as f:
                    raw = f.read().strip()
                    for token in raw.split():
                        if token.startswith("[") and token.endswith("]"):
                            thp = token[1:-1]
                            break
            except Exception:
                pass

        return KernelInfo(
            vm_max_map_count=max_map,
            vm_max_map_count_passed=max_map >= 262144,
            ulimit_nofile_soft=nofile_soft,
            thp_enabled=thp,
        )


class KernelProbe(BaseProbe):
    name = "kernel"

    def __init__(self):
        self._inspector = KernelInspector()

    def probe(self) -> KernelInfo:
        return self._inspector.probe()
