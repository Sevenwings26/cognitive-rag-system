"""Multi-GPU and device slot allocator."""

from typing import List, Dict, Any
from .models import ModelSpec


class GPUAllocator:
    """Determines tensor parallel splitting across GPU bus slots."""

    def calculate_tp_distribution(self, total_gpus: int, required_vram_gb: float) -> int:
        if total_gpus <= 1:
            return 1
        for tp in [1, 2, 4, 8]:
            if tp <= total_gpus:
                return tp
        return total_gpus
