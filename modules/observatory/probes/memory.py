"""System RAM and swap memory probe."""

import psutil
from .base import BaseProbe, MemoryInfo


class MemoryInspector:
    def probe(self) -> MemoryInfo:
        v = psutil.virtual_memory()
        s = psutil.swap_memory()
        return MemoryInfo(
            total_ram_gb=round(v.total / (1024 ** 3), 2),
            available_ram_gb=round(v.available / (1024 ** 3), 2),
            used_ram_gb=round(v.used / (1024 ** 3), 2),
            swap_total_gb=round(s.total / (1024 ** 3), 2),
            swap_free_gb=round(s.free / (1024 ** 3), 2),
        )


class MemoryProbe(BaseProbe):
    name = "memory"

    def __init__(self):
        self._inspector = MemoryInspector()

    def probe(self) -> MemoryInfo:
        return self._inspector.probe()
