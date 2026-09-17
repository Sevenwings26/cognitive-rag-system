"""Base probe contracts and models for hardware inspection."""

from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional


@dataclass
class GPUInfo:
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
    host_reserve_gb: float = 4.0
    gpu_memory_used_bytes: Optional[int] = None
    gpu_memory_total_bytes: Optional[int] = None
    gpu_memory_utilization_percent: Optional[float] = None
    gpu_compute_utilization_percent: Optional[float] = None


@dataclass
class GPUSummary:
    gpu_available: bool
    total_gpu_count: int
    gpus: List[GPUInfo] = field(default_factory=list)
    detection_method: str = "none"


@dataclass
class CPUInfo:
    architecture: str
    physical_cores: int
    logical_threads: int
    sockets: int
    model_name: str
    features: List[str] = field(default_factory=list)
    avx2_supported: bool = False
    avx512_supported: bool = False
    amx_supported: bool = False
    vnni_supported: bool = False


@dataclass
class MemoryInfo:
    total_ram_gb: float
    available_ram_gb: float
    used_ram_gb: float
    swap_total_gb: float
    swap_free_gb: float


@dataclass
class StorageInfo:
    mount_point: str
    total_gb: float
    free_gb: float
    used_gb: float


@dataclass
class KernelInfo:
    vm_max_map_count: int
    vm_max_map_count_passed: bool
    ulimit_nofile_soft: int
    thp_enabled: str


@dataclass
class PlatformInfo:
    system: str
    release: str
    machine: str
    is_wsl: bool
    is_container: bool


class BaseProbe(ABC):
    @property
    @abstractmethod
    def name(self) -> str:
        pass

    @abstractmethod
    def probe(self) -> Any:
        pass
