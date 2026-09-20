"""
Authoritative bounded routing enum and normalizer for Observatory APM telemetry.
Guarantees that only strictly vetted category tokens enter APM metrics, preventing
any leakage of user queries, document contents, SQL strings, or usernames.
"""

from __future__ import annotations

from core.telemetry import BackendRoute, normalize_backend_route


__all__ = ["BackendRoute", "normalize_backend_route"]
