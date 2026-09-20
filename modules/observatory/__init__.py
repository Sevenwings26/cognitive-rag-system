"""
modules/observatory — Enterprise APM and Observability subsystem for TechyzRAG.
Handles request metrics tracking, hardware inspection (/api/host-info),
live APM dashboard metrics (/api/observatory), and model auto-fitting (/api/autofit).
"""

from .buffer import get_observatory_buffer, ObservatoryBuffer
from .service import get_observatory_service, ObservatoryService
from .sampler import get_hardware_sampler, HardwareBackgroundSampler

__all__ = [
    "get_observatory_buffer",
    "ObservatoryBuffer",
    "get_observatory_service",
    "ObservatoryService",
    "get_hardware_sampler",
    "HardwareBackgroundSampler",
]
