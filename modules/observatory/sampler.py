"""
Background hardware telemetry sampler with multi-worker leader election.
Periodically samples CPU, RAM, and GPU utilization and writes into ObservatoryBuffer.
Uses Redis lease lock so only one worker process samples hardware in multi-worker environments.
"""

import json
import logging
import os
import threading
import time
import psutil
from typing import Optional
from .buffer import get_observatory_buffer
from .probes.gpu import GPUInspector

logger = logging.getLogger("techyz.observatory.sampler")


class HardwareBackgroundSampler:
    """Runs a low-overhead background thread sampling host utilization every 5 seconds."""

    def __init__(self, interval_seconds: Optional[float] = None):
        from core.config import settings

        configured_interval = settings.observatory_sample_interval_seconds
        self._interval = float(
            configured_interval if interval_seconds is None else interval_seconds
        )
        self._running = False
        self._thread: Optional[threading.Thread] = None
        self._gpu_inspector = GPUInspector()
        self._buffer = get_observatory_buffer()
        self._no_gpu_detected: bool = False
        self._pid = os.getpid()

    def _get_redis(self):
        try:
            from core.redis_client import get_redis
            return get_redis()
        except Exception:
            return None

    def start(self) -> None:
        if self._running:
            return
        self._running = True
        self._thread = threading.Thread(target=self._sample_loop, daemon=True, name="observatory-sampler")
        self._thread.start()
        logger.info("HardwareBackgroundSampler started (interval=%.1fs, pid=%d).", self._interval, self._pid)

    def stop(self) -> None:
        self._running = False
        if self._thread and self._thread.is_alive():
            self._thread.join(timeout=2.0)
        logger.info("HardwareBackgroundSampler stopped (pid=%d).", self._pid)

    def _sample_loop(self) -> None:
        while self._running:
            try:
                self.sample_once()
            except Exception as exc:
                logger.debug("Hardware sampling error: %s", exc)
            time.sleep(self._interval)

    def sample_once(self) -> None:
        # Multi-worker election: check if we should run or synchronize
        r = self._get_redis()
        if r is not None:
            try:
                from core.config import settings

                prefix = settings.observatory_redis_prefix
                lock_key = f"{prefix}:sampler_leader"
                lease_seconds = max(10, int(self._interval * 3))
                claimed = r.set(
                    lock_key, str(self._pid), nx=True, ex=lease_seconds
                )
                if not claimed:
                    leader_pid = r.get(lock_key)
                    if str(leader_pid) != str(self._pid):
                        # Another worker is the elected leader: sync latest hardware stats
                        latest_raw = r.get(f"{prefix}:hardware:latest")
                        if latest_raw:
                            hw = json.loads(latest_raw)
                            self._buffer.record_hardware_sample(
                                cpu_pct=hw["cpu_pct"],
                                cores=hw["cores"],
                                load_avg_1m=hw["load_avg_1m"],
                                ram_used_gb=hw["ram_used_gb"],
                                ram_total_gb=hw["ram_total_gb"],
                                gpu_memory_used_bytes=hw.get("gpu_memory_used_bytes"),
                                gpu_memory_total_bytes=hw.get("gpu_memory_total_bytes"),
                                gpu_memory_utilization_percent=hw.get("gpu_memory_utilization_percent", hw.get("gpu_pct")),
                                gpu_compute_utilization_percent=hw.get("gpu_compute_utilization_percent"),
                                gpu_name=hw.get("gpu_name"),
                            )
                        return
                    else:
                        # We are leader, renew lease
                        r.expire(lock_key, lease_seconds)
            except Exception as e:
                logger.debug("Redis sampler leader election error: %s", e)

        # CPU
        cpu_pct = psutil.cpu_percent(interval=None)
        cores = psutil.cpu_count(logical=True) or 1
        load_avg = 0.0
        try:
            load_avg = os.getloadavg()[0]
        except (AttributeError, OSError):
            load_avg = cpu_pct / 100.0 * cores

        # RAM
        v = psutil.virtual_memory()
        used_gb = v.used / (1024 ** 3)
        total_gb = v.total / (1024 ** 3)

        # GPU telemetry (separating VRAM memory metrics from compute processor utilization)
        gpu_memory_used_bytes: Optional[int] = None
        gpu_memory_total_bytes: Optional[int] = None
        gpu_memory_utilization_percent: Optional[float] = None
        gpu_compute_utilization_percent: Optional[float] = None
        gpu_name = "None (CPU Only)" if self._no_gpu_detected else "GPU Accelerator"

        if not self._no_gpu_detected:
            try:
                summary = self._gpu_inspector.probe()
                if summary.gpu_available and summary.gpus:
                    g = summary.gpus[0]
                    gpu_name = g.model_name
                    gpu_memory_used_bytes = g.gpu_memory_used_bytes
                    gpu_memory_total_bytes = g.gpu_memory_total_bytes
                    gpu_memory_utilization_percent = g.gpu_memory_utilization_percent
                    gpu_compute_utilization_percent = g.gpu_compute_utilization_percent

                    if gpu_memory_total_bytes is None and g.vram_total_gb > 0:
                        gpu_memory_total_bytes = int(g.vram_total_gb * (1024 ** 3))
                        avail_bytes = int(g.vram_available_gb * (1024 ** 3))
                        gpu_memory_used_bytes = max(0, gpu_memory_total_bytes - avail_bytes)
                        gpu_memory_utilization_percent = round((gpu_memory_used_bytes / gpu_memory_total_bytes) * 100.0, 1)
                else:
                    self._no_gpu_detected = True
                    gpu_name = "None (CPU Only)"
            except Exception:
                pass

        self._buffer.record_hardware_sample(
            cpu_pct=cpu_pct,
            cores=cores,
            load_avg_1m=load_avg,
            ram_used_gb=used_gb,
            ram_total_gb=total_gb,
            gpu_memory_used_bytes=gpu_memory_used_bytes,
            gpu_memory_total_bytes=gpu_memory_total_bytes,
            gpu_memory_utilization_percent=gpu_memory_utilization_percent,
            gpu_compute_utilization_percent=gpu_compute_utilization_percent,
            gpu_name=gpu_name,
        )

        # Broadcast latest hardware state to other workers via Redis
        if r is not None:
            try:
                payload = json.dumps({
                    "cpu_pct": cpu_pct,
                    "cores": cores,
                    "load_avg_1m": load_avg,
                    "ram_used_gb": used_gb,
                    "ram_total_gb": total_gb,
                    "gpu_memory_used_bytes": gpu_memory_used_bytes,
                    "gpu_memory_total_bytes": gpu_memory_total_bytes,
                    "gpu_memory_utilization_percent": gpu_memory_utilization_percent,
                    "gpu_compute_utilization_percent": gpu_compute_utilization_percent,
                    "gpu_pct": gpu_memory_utilization_percent,
                    "gpu_name": gpu_name,
                })
                from core.config import settings

                r.set(
                    f"{settings.observatory_redis_prefix}:hardware:latest",
                    payload,
                    ex=max(15, int(self._interval * 3)),
                )
            except Exception:
                pass


_GLOBAL_SAMPLER: Optional[HardwareBackgroundSampler] = None
_SAMPLER_LOCK = threading.Lock()


def get_hardware_sampler() -> HardwareBackgroundSampler:
    global _GLOBAL_SAMPLER
    with _SAMPLER_LOCK:
        if _GLOBAL_SAMPLER is None:
            _GLOBAL_SAMPLER = HardwareBackgroundSampler()
        return _GLOBAL_SAMPLER
