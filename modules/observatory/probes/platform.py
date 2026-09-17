"""Platform, OS, and virtualization environment probe."""

import os
import platform
from .base import BaseProbe, PlatformInfo


class PlatformInspector:
    def probe(self) -> PlatformInfo:
        sys_name = platform.system()
        release = platform.release()
        machine = platform.machine()
        is_wsl = "microsoft" in release.lower() or "wsl" in release.lower()
        is_container = os.path.exists("/.dockerenv") or (
            os.path.exists("/proc/1/cgroup") and "docker" in open("/proc/1/cgroup", "r", errors="ignore").read()
        )

        return PlatformInfo(
            system=sys_name,
            release=release,
            machine=machine,
            is_wsl=is_wsl,
            is_container=bool(is_container),
        )


class PlatformProbe(BaseProbe):
    name = "platform"

    def __init__(self):
        self._inspector = PlatformInspector()

    def probe(self) -> PlatformInfo:
        return self._inspector.probe()
