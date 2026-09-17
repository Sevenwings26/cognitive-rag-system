"""Storage partition utilization probe."""

import shutil
from .base import BaseProbe, StorageInfo


class StorageInspector:
    def probe(self, mount_point: str = "/") -> StorageInfo:
        try:
            usage = shutil.disk_usage(mount_point)
            return StorageInfo(
                mount_point=mount_point,
                total_gb=round(usage.total / (1024 ** 3), 2),
                free_gb=round(usage.free / (1024 ** 3), 2),
                used_gb=round(usage.used / (1024 ** 3), 2),
            )
        except Exception:
            return StorageInfo(mount_point=mount_point, total_gb=0.0, free_gb=0.0, used_gb=0.0)


class StorageProbe(BaseProbe):
    name = "storage"

    def __init__(self):
        self._inspector = StorageInspector()

    def probe(self) -> StorageInfo:
        return self._inspector.probe()
