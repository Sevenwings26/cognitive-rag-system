"""
In-memory & Redis-backed sliding-window APM metric aggregator.
Provides real-time KPI rollups, latency percentiles (avg/p95),
concurrency tracking, and timeline series for the Agent Observatory.
Supports single-process fallback and authoritative multi-worker Redis cluster aggregation.
"""

from __future__ import annotations

import collections
import json
import logging
import math
import os
import threading
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple, Union

from modules.observatory.routes import BackendRoute, normalize_backend_route

logger = logging.getLogger("techyz.observatory.buffer")

_PROMETHEUS_DURATION_BUCKETS = (
    0.1,
    0.25,
    0.5,
    1.0,
    2.5,
    5.0,
    10.0,
    30.0,
    60.0,
    120.0,
    300.0,
)

# Scanner / probe path indicators
_SCANNER_PATH_MARKERS = {
    ".env", "wp-login", "wp-admin", "actuator", "phpmyadmin",
    "cgi-bin", "shell", ".git", "solr", "telescope", ".well-known",
    "vendor", "drupal", "joomla", "admin.php"
}


def _format_uptime(seconds: float) -> str:
    days = int(seconds // 86400)
    hours = int((seconds % 86400) // 3600)
    minutes = int((seconds % 3600) // 60)
    if days > 0:
        return f"{days}d {hours}h"
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m {int(seconds % 60)}s"


class ObservatoryBuffer:
    """Thread-safe in-memory ring buffer and Redis-backed authoritative multi-worker metric accumulator."""

    def __init__(self, max_samples: int = 10000):
        self._lock = threading.RLock()
        self._start_time = time.time()
        self._max_samples = max_samples

        # In-flight concurrency (process-local fallback)
        self._current_concurrency = 0

        # Ring buffer of recent requests: (timestamp, duration_sec, status_code, route_type, path, is_system)
        self._request_samples: collections.deque = collections.deque(maxlen=max_samples)
        self._user_duration_count = 0
        self._user_duration_sum = 0.0
        self._user_duration_buckets = {
            upper_bound: 0 for upper_bound in _PROMETHEUS_DURATION_BUCKETS
        }

        # Hardware sample history: (timestamp, cpu_pct, ram_used_gb, ram_total_gb, gpu_pct, gpu_name)
        self._hardware_samples: collections.deque = collections.deque(maxlen=3600)

        # Concurrency sample history: (timestamp, concurrency)
        self._concurrency_samples: collections.deque = collections.deque(maxlen=3600)

        # Hourly bucket aggregates for historical charts: hour_key -> dict
        # hour_key: YYYY-MM-DDTHH:00:00Z
        self._hourly_buckets: Dict[str, Dict[str, Any]] = {}

        # Latest instantaneous hardware stats (None until first authoritative probe)
        self._latest_cpu_pct: Optional[float] = None
        self._latest_cpu_cores: int = os.cpu_count() or 4
        self._latest_load_avg: Optional[float] = None
        self._latest_ram_used_gb: Optional[float] = None
        self._latest_ram_total_gb: Optional[float] = None
        self._latest_ram_pct: Optional[float] = None
        self._latest_gpu_pct: Optional[float] = None
        self._latest_gpu_name: Optional[str] = None
        self._latest_gpu_memory_used_bytes: Optional[int] = None
        self._latest_gpu_memory_total_bytes: Optional[int] = None
        self._latest_gpu_memory_utilization_percent: Optional[float] = None
        self._latest_gpu_compute_utilization_percent: Optional[float] = None
        self._hardware_probed: bool = False

        # Model / service allocation: initialized to empty (NO fabricated allocations)
        self._service_allocations: List[Dict[str, Any]] = []

        # Active AlertManager alerts state tracking: fingerprint -> alert_dict
        self._active_alerts: Dict[str, Dict[str, Any]] = {}

        # Snapshot cache (2s TTL to prevent lock contention from rapid multi-tab polling)
        self._cached_metrics: Optional[Dict[str, Any]] = None
        self._cached_metrics_ts: float = 0.0
        self._cached_metrics_key: str = ""

    @property
    def _prefix(self) -> str:
        try:
            from core.config import settings
            return getattr(settings, "observatory_redis_prefix", "techyz:observatory")
        except Exception:
            return "techyz:observatory"

    def _get_redis(self):
        try:
            from core.redis_client import get_redis
            return get_redis()
        except Exception:
            return None

    # --- Ingress Concurrency Tracking with Self-Healing Leases ---
    def increment_concurrency(self, request_id: Optional[str] = None) -> int:
        req_id = request_id or uuid.uuid4().hex[:16]
        now = time.time()
        r = self._get_redis()
        if r is not None:
            try:
                pipe = r.pipeline()
                lease_seconds = self._concurrency_lease_seconds()
                pipe.zremrangebyscore(
                    f"{self._prefix}:concurrency:active", 0, now - lease_seconds
                )
                pipe.zadd(f"{self._prefix}:concurrency:active", {req_id: now})
                pipe.expire(
                    f"{self._prefix}:concurrency:active", int(lease_seconds * 2)
                )
                pipe.zcard(f"{self._prefix}:concurrency:active")
                results = pipe.execute()
                val = int(results[3])
                with self._lock:
                    self._current_concurrency = val
                    self._concurrency_samples.append((now, val))
                return val
            except Exception as e:
                logger.debug("Redis concurrency increment failed: %s", e)

        with self._lock:
            self._current_concurrency += 1
            now = time.time()
            self._concurrency_samples.append((now, self._current_concurrency))
            return self._current_concurrency

    def decrement_concurrency(self, request_id: Optional[str] = None) -> int:
        now = time.time()
        r = self._get_redis()
        if r is not None:
            try:
                pipe = r.pipeline()
                if request_id:
                    pipe.zrem(f"{self._prefix}:concurrency:active", request_id)
                pipe.zremrangebyscore(
                    f"{self._prefix}:concurrency:active",
                    0,
                    now - self._concurrency_lease_seconds(),
                )
                pipe.zcard(f"{self._prefix}:concurrency:active")
                results = pipe.execute()
                idx = 2 if request_id else 1
                val = int(results[idx])
                with self._lock:
                    self._current_concurrency = val
                    self._concurrency_samples.append((now, val))
                return val
            except Exception as e:
                logger.debug("Redis concurrency decrement failed: %s", e)

        with self._lock:
            self._current_concurrency = max(0, self._current_concurrency - 1)
            now = time.time()
            self._concurrency_samples.append((now, self._current_concurrency))
            return self._current_concurrency

    @property
    def current_concurrency(self) -> int:
        r = self._get_redis()
        if r is not None:
            try:
                now = time.time()
                r.zremrangebyscore(
                    f"{self._prefix}:concurrency:active",
                    0,
                    now - self._concurrency_lease_seconds(),
                )
                val = r.zcard(f"{self._prefix}:concurrency:active")
                if val is not None:
                    return max(0, int(val))
            except Exception:
                pass
        with self._lock:
            return self._current_concurrency

    # --- Authoritative Chat-Turn Route Recording ---
    def record_turn_route(
        self,
        route: Union[str, BackendRoute],
        request_id: Optional[str] = None,
    ) -> BackendRoute:
        """
        Record an authoritative chat turn route decision directly from the orchestration seam.
        Only accepts bounded BackendRoute values (never user text, SQL strings, or document text).
        Increments route fields in buckets without double-counting HTTP requests.
        """
        canonical_route = normalize_backend_route(route)
        route_str = canonical_route.value
        now = time.time()
        dt_utc = datetime.fromtimestamp(now, tz=timezone.utc)
        hour_key = dt_utc.strftime("%Y-%m-%dT%H:00:00Z")
        min_key = dt_utc.strftime("%Y-%m-%dT%H:%M:00Z")

        with self._lock:
            self._cached_metrics = None
            if hour_key not in self._hourly_buckets:
                self._hourly_buckets[hour_key] = {
                    "timestamp": hour_key,
                    "total": 0,
                    "user": 0,
                    "system": 0,
                    "rag": 0,
                    "sql": 0,
                    "general": 0,
                    "attachments": 0,
                    "denied": 0,
                    "classification_failure": 0,
                    "app_errors": 0,
                    "scanner_errors": 0,
                }
            hb = self._hourly_buckets[hour_key]
            hb[route_str] = hb.get(route_str, 0) + 1

        r = self._get_redis()
        if r is not None:
            try:
                pipe = r.pipeline()
                min_ts = dt_utc.replace(second=0, microsecond=0).timestamp()
                hour_ts = dt_utc.replace(minute=0, second=0, microsecond=0).timestamp()

                redis_min_key = f"{self._prefix}:bucket:min:{min_key}"
                pipe.hincrby(redis_min_key, route_str, 1)
                pipe.expire(redis_min_key, 10800)
                pipe.zadd(f"{self._prefix}:index:min_keys", {redis_min_key: min_ts})

                redis_hour_key = f"{self._prefix}:bucket:hour:{hour_key}"
                pipe.hincrby(redis_hour_key, route_str, 1)
                pipe.expire(redis_hour_key, 3024000)
                pipe.zadd(f"{self._prefix}:index:hour_keys", {redis_hour_key: hour_ts})

                pipe.execute()
            except Exception as exc:
                logger.debug("Redis record_turn_route failed: %s", exc)

        return canonical_route

    # --- Ingress HTTP Request Recording ---
    def record_request(
        self,
        path: str,
        duration_sec: float,
        status_code: int,
        route_type: Optional[str] = None,
        is_system: bool = False,
        traffic_kind: str = "user",
        request_id: Optional[str] = None,
    ) -> None:
        """
        Record HTTP-level ingress request metrics (total requests, user requests,
        latencies, 5xx errors, and scanner probes).
        Does NOT guess business route from URL path to avoid misclassifying chat turns.
        """
        now = time.time()
        lowered_path = path.lower()

        # Classify only non-chat HTTP-level conditions if not explicitly supplied
        if not route_type:
            if any(marker in lowered_path for marker in _SCANNER_PATH_MARKERS):
                route_type = "scanner"
            elif status_code in (401, 403):
                route_type = "denied"
            else:
                route_type = None

        if route_type:
            route_type = normalize_backend_route(route_type).value

        sample = (now, duration_sec, status_code, route_type, path, is_system)
        dt_utc = datetime.fromtimestamp(now, tz=timezone.utc)
        hour_key = dt_utc.strftime("%Y-%m-%dT%H:00:00Z")
        min_key = dt_utc.strftime("%Y-%m-%dT%H:%M:00Z")

        with self._lock:
            self._request_samples.append(sample)
            self._cached_metrics = None
            if not is_system:
                self._user_duration_count += 1
                self._user_duration_sum += duration_sec
                for upper_bound in _PROMETHEUS_DURATION_BUCKETS:
                    if duration_sec <= upper_bound:
                        self._user_duration_buckets[upper_bound] += 1

            # Update process-local hourly bucket
            if hour_key not in self._hourly_buckets:
                self._hourly_buckets[hour_key] = {
                    "timestamp": hour_key,
                    "total": 0,
                    "user": 0,
                    "system": 0,
                    "rag": 0,
                    "sql": 0,
                    "general": 0,
                    "attachments": 0,
                    "denied": 0,
                    "classification_failure": 0,
                    "app_errors": 0,
                    "scanner_errors": 0,
                }
            bucket = self._hourly_buckets[hour_key]
            bucket["total"] += 1
            if is_system:
                bucket["system"] = bucket.get("system", 0) + 1
            else:
                bucket["user"] = bucket.get("user", 0) + 1

            if route_type and route_type in bucket:
                bucket[route_type] = bucket.get(route_type, 0) + 1

            if not is_system and status_code >= 500:
                bucket["app_errors"] = bucket.get("app_errors", 0) + 1
            elif route_type == "scanner" or (status_code == 404 and "/api/" not in lowered_path):
                bucket["scanner_errors"] = bucket.get("scanner_errors", 0) + 1

            # Prune old hourly buckets (keep last 30 days = 720 hours)
            if len(self._hourly_buckets) > 750:
                sorted_keys = sorted(self._hourly_buckets.keys())
                for k in sorted_keys[:-720]:
                    del self._hourly_buckets[k]

        # Multi-worker authoritative Redis storage
        r = self._get_redis()
        if r is not None:
            try:
                pipe = r.pipeline()
                # Monotonic total counters
                pipe.incr(f"{self._prefix}:requests:total")
                if is_system:
                    pipe.incr(f"{self._prefix}:requests:system")
                else:
                    pipe.incr(f"{self._prefix}:requests:user")
                    req_id = request_id or uuid.uuid4().hex[:16]
                    member = f"{now:.4f}:{duration_sec:.4f}:{req_id}"
                    pipe.zadd(f"{self._prefix}:durations:user", {member: now})
                    pipe.zremrangebyscore(f"{self._prefix}:durations:user", 0, now - (30 * 86400))
                    if status_code >= 500:
                        pipe.incr(f"{self._prefix}:app_errors")
                    pipe.incr(f"{self._prefix}:duration:count")
                    pipe.incrbyfloat(f"{self._prefix}:duration:sum", duration_sec)
                    for upper_bound in _PROMETHEUS_DURATION_BUCKETS:
                        if duration_sec <= upper_bound:
                            pipe.incr(
                                f"{self._prefix}:duration:bucket:{upper_bound:g}"
                            )

                if route_type == "scanner" or (status_code == 404 and "/api/" not in lowered_path):
                    pipe.incr(f"{self._prefix}:scanner_errors")

                # Minute bucket
                min_ts = dt_utc.replace(second=0, microsecond=0).timestamp()
                redis_min_key = f"{self._prefix}:bucket:min:{min_key}"
                pipe.hincrby(redis_min_key, "total", 1)
                pipe.hincrby(redis_min_key, "system" if is_system else "user", 1)
                if route_type:
                    pipe.hincrby(redis_min_key, route_type, 1)
                if not is_system and status_code >= 500:
                    pipe.hincrby(redis_min_key, "app_errors", 1)
                if route_type == "scanner" or (status_code == 404 and "/api/" not in lowered_path):
                    pipe.hincrby(redis_min_key, "scanner_errors", 1)
                pipe.expire(redis_min_key, 10800)
                pipe.zadd(f"{self._prefix}:index:min_keys", {redis_min_key: min_ts})

                # Hourly bucket
                hour_ts = dt_utc.replace(minute=0, second=0, microsecond=0).timestamp()
                redis_hour_key = f"{self._prefix}:bucket:hour:{hour_key}"
                pipe.hincrby(redis_hour_key, "total", 1)
                pipe.hincrby(redis_hour_key, "system" if is_system else "user", 1)
                if route_type:
                    pipe.hincrby(redis_hour_key, route_type, 1)
                if not is_system and status_code >= 500:
                    pipe.hincrby(redis_hour_key, "app_errors", 1)
                if route_type == "scanner" or (status_code == 404 and "/api/" not in lowered_path):
                    pipe.hincrby(redis_hour_key, "scanner_errors", 1)
                pipe.expire(redis_hour_key, 3024000)
                pipe.zadd(f"{self._prefix}:index:hour_keys", {redis_hour_key: hour_ts})

                # Pruning index sets
                pipe.zremrangebyscore(f"{self._prefix}:index:min_keys", 0, now - 10800)
                pipe.zremrangebyscore(f"{self._prefix}:index:hour_keys", 0, now - 3024000)

                pipe.execute()
            except Exception as exc:
                logger.debug("Redis request record failed: %s", exc)

    # --- Hardware Sample Recording ---
    def record_hardware_sample(
        self,
        cpu_pct: float,
        cores: int,
        load_avg_1m: float,
        ram_used_gb: float,
        ram_total_gb: float,
        gpu_memory_used_bytes: Optional[int] = None,
        gpu_memory_total_bytes: Optional[int] = None,
        gpu_memory_utilization_percent: Optional[float] = None,
        gpu_compute_utilization_percent: Optional[float] = None,
        gpu_name: Optional[str] = "GPU Accelerator",
        gpu_pct: Optional[float] = None,
    ) -> None:
        now = time.time()
        ram_pct = round((ram_used_gb / ram_total_gb * 100.0) if ram_total_gb > 0 else 0.0, 1)

        # Backwards compatibility: map legacy gpu_pct keyword if passed
        if gpu_memory_utilization_percent is None and gpu_pct is not None:
            gpu_memory_utilization_percent = gpu_pct

        with self._lock:
            self._latest_cpu_pct = round(cpu_pct, 1) if cpu_pct is not None else None
            self._latest_cpu_cores = cores
            self._latest_load_avg = round(load_avg_1m, 2) if load_avg_1m is not None else None
            self._latest_ram_used_gb = round(ram_used_gb, 2) if ram_used_gb is not None else None
            self._latest_ram_total_gb = round(ram_total_gb, 2) if ram_total_gb is not None else None
            self._latest_ram_pct = ram_pct
            self._latest_gpu_memory_used_bytes = int(gpu_memory_used_bytes) if gpu_memory_used_bytes is not None else None
            self._latest_gpu_memory_total_bytes = int(gpu_memory_total_bytes) if gpu_memory_total_bytes is not None else None
            self._latest_gpu_memory_utilization_percent = round(gpu_memory_utilization_percent, 1) if gpu_memory_utilization_percent is not None else None
            self._latest_gpu_compute_utilization_percent = round(gpu_compute_utilization_percent, 1) if gpu_compute_utilization_percent is not None else None
            self._latest_gpu_pct = self._latest_gpu_memory_utilization_percent
            self._latest_gpu_name = gpu_name
            self._hardware_probed = True
            self._hardware_samples.append((
                now,
                cpu_pct,
                ram_used_gb,
                ram_total_gb,
                self._latest_gpu_memory_used_bytes,
                self._latest_gpu_memory_total_bytes,
                self._latest_gpu_memory_utilization_percent,
                self._latest_gpu_compute_utilization_percent,
                gpu_name,
            ))

    def update_service_allocations(self, allocations: List[Dict[str, Any]]) -> None:
        with self._lock:
            self._service_allocations = allocations

    # --- Range Window Definition ---
    def _range_window(self, range_key: str) -> Tuple[float, float, str]:
        now = time.time()
        key = (range_key or "").lower().strip()
        if key == "15m":
            return now - 900.0, now, "min"
        elif key == "1h":
            return now - 3600.0, now, "min"
        elif key == "6h":
            return now - 21600.0, now, "hour"
        elif key == "24h":
            return now - 86400.0, now, "hour"
        elif key == "today":
            now_dt = datetime.now(timezone.utc)
            start_of_utc_day = now_dt.replace(hour=0, minute=0, second=0, microsecond=0)
            return start_of_utc_day.timestamp(), now, "hour"
        elif key == "7d":
            return now - (7 * 86400.0), now, "hour"
        elif key in ("30d", "all_time"):
            return now - (30 * 86400.0), now, "hour"
        raise ValueError(f"Unsupported time range: {range_key}")

    # --- Aggregation & Metrics ---
    def get_metrics(
        self,
        range_key: str = "7d",
        active_sessions_count: Optional[int] = None,
        force_refresh: bool = False,
    ) -> Dict[str, Any]:
        cutoff, now_time, granularity = self._range_window(range_key)
        uptime_seconds = round(now_time - self._start_time, 1)

        cache_key = f"{range_key}:{active_sessions_count}"
        with self._lock:
            if not force_refresh and self._cached_metrics and self._cached_metrics_key == cache_key:
                if now_time - self._cached_metrics_ts < 2.0:
                    return dict(self._cached_metrics)

        # Attempt authoritative multi-worker Redis query
        r = self._get_redis()
        redis_success = False
        redis_failed = False

        if active_sessions_count is None and r is not None:
            try:
                active_sessions_count = sum(
                    1 for _ in r.scan_iter(match="api_token:*", count=100)
                )
            except Exception as exc:
                logger.debug("Could not query active sessions from Redis: %s", exc)
                active_sessions_count = None

        total_requests = 0
        user_requests = 0
        system_requests = 0
        app_errors = 0
        scanner_errors = 0
        rag_count = 0
        sql_count = 0
        general_count = 0
        attachments_count = 0
        denied_count = 0
        classification_failure_count = 0
        avg_resp_time: Optional[float] = None
        p95_resp_time: Optional[float] = None
        backend_split_series: List[Dict[str, Any]] = []
        response_time_series: List[Dict[str, Any]] = []

        if r is not None:
            try:
                # 1. Query windowed user durations from Redis sorted set
                dur_items = r.zrangebyscore(f"{self._prefix}:durations:user", cutoff, "+inf")
                user_durs: List[float] = []
                dur_by_bucket: Dict[str, List[float]] = {}
                for item in dur_items:
                    parts = item.split(":")
                    if len(parts) >= 2:
                        try:
                            ts_f = float(parts[0])
                            dur_f = float(parts[1])
                            user_durs.append(dur_f)
                            if granularity == "min":
                                b_ts = datetime.fromtimestamp(ts_f, tz=timezone.utc).strftime("%H:%M")
                            else:
                                b_ts = datetime.fromtimestamp(ts_f, tz=timezone.utc).strftime("%H:00")
                            if b_ts not in dur_by_bucket:
                                dur_by_bucket[b_ts] = []
                            dur_by_bucket[b_ts].append(dur_f)
                        except ValueError:
                            pass

                if user_durs:
                    user_durs.sort()
                    avg_resp_time = round(sum(user_durs) / len(user_durs), 3)
                    p95_idx = min(len(user_durs) - 1, int(math.ceil(0.95 * len(user_durs))) - 1)
                    p95_resp_time = round(user_durs[p95_idx], 3)

                # 2. Query range-bucket aggregates
                index_key = f"{self._prefix}:index:min_keys" if granularity == "min" else f"{self._prefix}:index:hour_keys"
                bucket_keys = r.zrangebyscore(index_key, cutoff, "+inf")

                if bucket_keys:
                    pipe = r.pipeline()
                    for b_k in bucket_keys:
                        pipe.hgetall(b_k)
                    bucket_results = pipe.execute()

                    for b_k, b_hash in zip(bucket_keys, bucket_results):
                        if not b_hash:
                            continue
                        ts_label = b_k.split(":")[-1]

                        b_tot = int(b_hash.get("total", 0))
                        b_usr = int(b_hash.get("user", 0))
                        b_sys = int(b_hash.get("system", 0))
                        b_rag = int(b_hash.get("rag", 0))
                        b_sql = int(b_hash.get("sql", 0))
                        b_gen = int(b_hash.get("general", 0))
                        b_att = int(b_hash.get("attachments", 0))
                        b_den = int(b_hash.get("denied", 0))
                        b_clf = int(b_hash.get("classification_failure", 0))
                        b_app = int(b_hash.get("app_errors", 0))
                        b_scn = int(b_hash.get("scanner_errors", 0))

                        total_requests += b_tot
                        user_requests += b_usr
                        system_requests += b_sys
                        rag_count += b_rag
                        sql_count += b_sql
                        general_count += b_gen
                        attachments_count += b_att
                        denied_count += b_den
                        classification_failure_count += b_clf
                        app_errors += b_app
                        scanner_errors += b_scn

                        backend_split_series.append({
                            "timestamp": ts_label,
                            "rag": b_rag,
                            "sql": b_sql,
                            "general": b_gen,
                            "attachments": b_att,
                        })

                # 3. Response time series with actual time bucketing (no duplicate avg=p95 on single sample)
                for b_ts in sorted(dur_by_bucket.keys()):
                    b_durs = dur_by_bucket[b_ts]
                    s_count = len(b_durs)
                    b_avg = round(sum(b_durs) / s_count, 3)
                    if s_count == 1:
                        b_p95 = None
                    else:
                        sorted_bd = sorted(b_durs)
                        idx = min(s_count - 1, int(math.ceil(0.95 * s_count)) - 1)
                        b_p95 = round(sorted_bd[idx], 3)
                    response_time_series.append({
                        "timestamp": b_ts,
                        "sample_count": s_count,
                        "avg": b_avg,
                        "p95": b_p95,
                    })

                redis_success = True
            except Exception as exc:
                logger.warning("Redis cluster APM aggregation failed: %s; falling back to single-process metrics", exc)
                redis_failed = True

        # Process-local fallback if Redis is absent or failed
        if not redis_success:
            with self._lock:
                samples = [s for s in self._request_samples if s[0] >= cutoff]
                total_requests = len(samples)
                user_samples = [s for s in samples if len(s) < 6 or not s[5]]
                system_samples = [s for s in samples if len(s) >= 6 and s[5]]

                user_requests = len(user_samples)
                system_requests = len(system_samples)

                user_durations = [s[1] for s in user_samples]
                if user_durations:
                    sorted_d = sorted(user_durations)
                    avg_resp_time = round(sum(sorted_d) / len(sorted_d), 3)
                    p95_index = min(len(sorted_d) - 1, int(math.ceil(0.95 * len(sorted_d))) - 1)
                    p95_resp_time = round(sorted_d[p95_index], 3)
                else:
                    avg_resp_time = None
                    p95_resp_time = None

                app_errors = sum(1 for s in user_samples if s[2] >= 500)
                scanner_errors = sum(1 for s in samples if s[3] == "scanner" or (s[2] == 404 and "/api/" not in s[4]))

                rag_count = 0
                sql_count = 0
                general_count = 0
                attachments_count = 0
                denied_count = 0
                classification_failure_count = 0

                backend_split_series = []
                for hour_key in sorted(self._hourly_buckets.keys()):
                    b = self._hourly_buckets[hour_key]
                    b_ts = datetime.fromisoformat(hour_key.replace("Z", "+00:00")).timestamp()
                    if b_ts >= cutoff:
                        rag_count += b.get("rag", 0)
                        sql_count += b.get("sql", 0)
                        general_count += b.get("general", 0)
                        attachments_count += b.get("attachments", 0)
                        denied_count += b.get("denied", 0)
                        classification_failure_count += b.get("classification_failure", 0)
                        backend_split_series.append({
                            "timestamp": hour_key,
                            "rag": b.get("rag", 0),
                            "sql": b.get("sql", 0),
                            "general": b.get("general", 0),
                            "attachments": b.get("attachments", 0),
                        })

                # Time-bucketed response time series in local fallback
                local_dur_by_bucket: Dict[str, List[float]] = {}
                for s in user_samples:
                    ts_f = s[0]
                    dur_f = s[1]
                    if granularity == "min":
                        b_ts = datetime.fromtimestamp(ts_f, tz=timezone.utc).strftime("%H:%M")
                    else:
                        b_ts = datetime.fromtimestamp(ts_f, tz=timezone.utc).strftime("%H:00")
                    if b_ts not in local_dur_by_bucket:
                        local_dur_by_bucket[b_ts] = []
                    local_dur_by_bucket[b_ts].append(dur_f)

                response_time_series = []
                for b_ts in sorted(local_dur_by_bucket.keys()):
                    b_list = local_dur_by_bucket[b_ts]
                    s_count = len(b_list)
                    b_avg = round(sum(b_list) / s_count, 3)
                    if s_count == 1:
                        b_p95 = None
                    else:
                        sorted_bl = sorted(b_list)
                        idx = min(s_count - 1, int(math.ceil(0.95 * s_count)) - 1)
                        b_p95 = round(sorted_bl[idx], 3)
                    response_time_series.append({
                        "timestamp": b_ts,
                        "sample_count": s_count,
                        "avg": b_avg,
                        "p95": b_p95,
                    })

        # Calculate percentages
        app_error_rate = (
            round(app_errors / user_requests * 100.0, 1)
            if user_requests > 0
            else None
        )
        ext_error_rate = (
            round(scanner_errors / total_requests * 100.0, 1)
            if total_requests > 0
            else None
        )

        route_total = rag_count + sql_count
        if route_total > 0:
            rag_pct = round(rag_count / route_total * 100.0, 1)
            sql_pct = round(sql_count / route_total * 100.0, 1)
        else:
            rag_pct = None
            sql_pct = None

        # Concurrency & hardware timeline series (strictly windowed, empty when no samples)
        with self._lock:
            concurrency_series = []
            concurrency_window = [c for c in self._concurrency_samples if c[0] >= cutoff]
            step_c = concurrency_window[-30:] if len(concurrency_window) > 30 else concurrency_window
            for c in step_c:
                concurrency_series.append({
                    "timestamp": datetime.fromtimestamp(c[0], tz=timezone.utc).strftime("%H:%M"),
                    "concurrency": c[1],
                })

            host_util_series = []
            hw_window = [h for h in self._hardware_samples if h[0] >= cutoff]
            step_hw = hw_window[-30:] if len(hw_window) > 30 else hw_window
            for h in step_hw:
                if len(h) >= 9:
                    ts, cpu, r_used, r_tot, g_mem_used, g_mem_tot, g_mem_pct, g_comp_pct, g_name = h[:9]
                else:
                    ts, cpu, r_used, r_tot, g_pct, g_name = h[:6]
                    g_mem_used = None
                    g_mem_tot = None
                    g_mem_pct = g_pct
                    g_comp_pct = None

                host_util_series.append({
                    "timestamp": datetime.fromtimestamp(ts, tz=timezone.utc).strftime("%H:%M"),
                    "cpu_pct": round(cpu, 1) if cpu is not None else None,
                    "gpu_memory_used_bytes": g_mem_used,
                    "gpu_memory_total_bytes": g_mem_tot,
                    "gpu_memory_utilization_percent": round(g_mem_pct, 1) if g_mem_pct is not None else None,
                    "gpu_compute_utilization_percent": round(g_comp_pct, 1) if g_comp_pct is not None else None,
                    "gpu_pct": round(g_mem_pct, 1) if g_mem_pct is not None else None,
                    "gpu_name": g_name,
                    "status": "available",
                })

        # NO fabricated single-point fallbacks! Empty series remain empty []

        if redis_success:
            telemetry_status = {
                "aggregation_mode": "redis",
                "data_status": "available" if active_sessions_count is not None else "degraded",
                "retention": "30d",
                "timezone": "UTC",
                "concurrency_estimated": False,
                "worker_pid": os.getpid(),
                "message": None if active_sessions_count is not None else "Active session registry unavailable.",
            }
        elif redis_failed:
            telemetry_status = {
                "aggregation_mode": "single_process_fallback",
                "data_status": "partial",
                "retention": "30d",
                "timezone": "UTC",
                "concurrency_estimated": True,
                "worker_pid": os.getpid(),
                "message": "Redis cluster telemetry unavailable; active sessions unmeasured, falling back to single-worker in-memory metrics.",
            }
        else:
            telemetry_status = {
                "aggregation_mode": "single_process_fallback",
                "data_status": "degraded" if active_sessions_count is None else "single_process_fallback",
                "retention": "30d",
                "timezone": "UTC",
                "concurrency_estimated": False,
                "worker_pid": os.getpid(),
                "message": "Single-process mode (Redis client not configured)." if active_sessions_count is not None else "Redis client not configured; active sessions unmeasured.",
            }

        payload = {
            "status": "ok",
            "range": range_key,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            "telemetry_status": telemetry_status,
            "kpis": {
                "uptime_seconds": uptime_seconds,
                "uptime_human": _format_uptime(uptime_seconds),
                "total_requests": total_requests,
                "user_requests": user_requests,
                "system_requests": system_requests,
                "concurrency_current": self.current_concurrency,
                "avg_response_time_sec": avg_resp_time,
                "p95_response_time_sec": p95_resp_time,
                "app_error_rate_pct": app_error_rate,
                "ext_error_rate_pct": ext_error_rate,
                "active_sessions": active_sessions_count,
            },
            "gauges": {
                "host_cpu": {
                    "percent": self._latest_cpu_pct,
                    "cores": self._latest_cpu_cores,
                    "load_average_1m": self._latest_load_avg,
                    "status": "available" if self._hardware_probed else "unavailable",
                },
                "host_memory": {
                    "percent": self._latest_ram_pct,
                    "used_gb": self._latest_ram_used_gb,
                    "total_gb": self._latest_ram_total_gb,
                    "status": "available" if self._hardware_probed else "unavailable",
                },
                "backend_split_total": {
                    "rag_percent": rag_pct,
                    "sql_percent": sql_pct,
                    "rag_total": rag_count,
                    "sql_total": sql_count,
                    "general_total": general_count,
                    "attachments_total": attachments_count,
                    "denied_total": denied_count,
                    "classification_failure_total": classification_failure_count,
                },
                "gpu_memory": {
                    "gpu_memory_used_bytes": self._latest_gpu_memory_used_bytes,
                    "gpu_memory_total_bytes": self._latest_gpu_memory_total_bytes,
                    "gpu_memory_utilization_percent": self._latest_gpu_memory_utilization_percent,
                    "status": "available" if self._latest_gpu_memory_total_bytes is not None else ("unavailable" if self._hardware_probed else "unprobed"),
                },
                "gpu_compute": {
                    "gpu_compute_utilization_percent": self._latest_gpu_compute_utilization_percent,
                    "status": "available" if self._latest_gpu_compute_utilization_percent is not None else "unavailable",
                },
            },
            "gpu_resource_allocation": self._service_allocations,
            "time_series": {
                "backend_split_over_time": backend_split_series,
                "response_time_over_time": response_time_series,
                "concurrency_over_time": concurrency_series,
                "host_utilization_over_time": host_util_series,
            },
        }

        with self._lock:
            self._cached_metrics = dict(payload)
            self._cached_metrics_ts = now_time
            self._cached_metrics_key = cache_key

        return payload

    def reset(self) -> None:
        """Reset in-memory request samples, concurrency, and buckets across local worker and Redis cluster."""
        with self._lock:
            self._request_samples.clear()
            self._user_duration_count = 0
            self._user_duration_sum = 0.0
            self._user_duration_buckets = {
                upper_bound: 0 for upper_bound in _PROMETHEUS_DURATION_BUCKETS
            }
            self._hourly_buckets.clear()
            self._concurrency_samples.clear()
            self._hardware_samples.clear()
            self._current_concurrency = 0
            self._latest_cpu_pct = None
            self._latest_load_avg = None
            self._latest_ram_used_gb = None
            self._latest_ram_total_gb = None
            self._latest_ram_pct = None
            self._latest_gpu_pct = None
            self._latest_gpu_name = None
            self._latest_gpu_memory_used_bytes = None
            self._latest_gpu_memory_total_bytes = None
            self._latest_gpu_memory_utilization_percent = None
            self._latest_gpu_compute_utilization_percent = None
            self._hardware_probed = False
            self._cached_metrics = None
            self._cached_metrics_ts = 0.0
            self._start_time = time.time()
            self._active_alerts.clear()

        r = self._get_redis()
        if r is not None:
            try:
                pattern = f"{self._prefix}:*"
                keys_to_del = []
                for k in r.scan_iter(match=pattern, count=100):
                    if not k.endswith(":sampler_leader"):
                        keys_to_del.append(k)
                    if len(keys_to_del) >= 100:
                        r.delete(*keys_to_del)
                        keys_to_del = []
                if keys_to_del:
                    r.delete(*keys_to_del)
            except Exception as e:
                logger.debug("Redis reset failed: %s", e)

    def record_alerts(self, alerts: List[Dict[str, Any]]) -> int:
        """
        Record incoming validated AlertManager alerts into active alerts registry.
        Syncs state to Redis hash 'techyz:observatory:active_alerts'.
        Returns count of currently active firing alerts.
        """
        import json
        with self._lock:
            for alert in alerts:
                status = alert.get("status", "firing")
                labels = alert.get("labels", {})
                fp = alert.get("fingerprint") or f"{labels.get('alertname', 'unknown')}_{labels.get('severity', 'unknown')}"
                if status == "firing":
                    self._active_alerts[fp] = {
                        "fingerprint": fp,
                        "alertname": labels.get("alertname", "UnknownAlert"),
                        "severity": labels.get("severity", "warning"),
                        "status": "firing",
                        "starts_at": alert.get("startsAt"),
                        "recorded_at": time.time(),
                    }
                elif status == "resolved":
                    self._active_alerts.pop(fp, None)

            # Sync to Redis if available
            r = self._get_redis()
            if r is not None:
                try:
                    redis_key = f"{self._prefix}:active_alerts"
                    pipe = r.pipeline()
                    for alert in alerts:
                        status = alert.get("status", "firing")
                        labels = alert.get("labels", {})
                        fp = alert.get("fingerprint") or f"{labels.get('alertname', 'unknown')}_{labels.get('severity', 'unknown')}"
                        if status == "firing":
                            pipe.hset(redis_key, fp, json.dumps(self._active_alerts.get(fp, {})))
                        elif status == "resolved":
                            pipe.hdel(redis_key, fp)
                    pipe.execute()
                except Exception as exc:
                    logger.debug("Failed to sync active alerts to Redis: %s", exc)

            return len(self._active_alerts)

    def get_active_alerts_count(self) -> int:
        """Get count of currently active firing alerts."""
        with self._lock:
            r = self._get_redis()
            if r is not None:
                try:
                    redis_key = f"{self._prefix}:active_alerts"
                    return r.hlen(redis_key)
                except Exception:
                    pass
            return len(self._active_alerts)

    # --- Prometheus Exposition Format ---
    def to_prometheus_text(self) -> str:
        return self._to_prometheus_text()

    get_prometheus_metrics = to_prometheus_text

    def _to_prometheus_text(self) -> str:
        with self._lock:
            uptime = time.time() - self._start_time
            total_reqs = len(self._request_samples)
            conc = self._current_concurrency
            duration_count = self._user_duration_count
            duration_sum = self._user_duration_sum
            duration_buckets = dict(self._user_duration_buckets)

        r = self._get_redis()
        if r is not None:
            try:
                r_tot = r.get(f"{self._prefix}:requests:total")
                if r_tot is not None:
                    total_reqs = int(r_tot)
                conc = self.current_concurrency
                count_value = r.get(f"{self._prefix}:duration:count")
                sum_value = r.get(f"{self._prefix}:duration:sum")
                duration_count = int(count_value or 0)
                duration_sum = float(sum_value or 0.0)
                duration_buckets = {
                    upper_bound: int(
                        r.get(
                            f"{self._prefix}:duration:bucket:{upper_bound:g}"
                        )
                        or 0
                    )
                    for upper_bound in _PROMETHEUS_DURATION_BUCKETS
                }
            except Exception as e:
                logger.debug("Redis prometheus text scrape failed: %s", e)

        lines = [
            "# HELP techyz_uptime_seconds Process uptime in seconds",
            "# TYPE techyz_uptime_seconds counter",
            f"techyz_uptime_seconds {uptime:.1f}",
            "",
            "# HELP techyz_http_requests_total Total HTTP requests tracked",
            "# TYPE techyz_http_requests_total counter",
            f"techyz_http_requests_total {total_reqs}",
            "",
            "# HELP techyz_concurrency_in_flight Current in-flight requests",
            "# TYPE techyz_concurrency_in_flight gauge",
            f"techyz_concurrency_in_flight {conc}",
            "",
            "# HELP techyz_http_request_duration_seconds User HTTP request duration in seconds",
            "# TYPE techyz_http_request_duration_seconds histogram",
        ]

        for upper_bound in _PROMETHEUS_DURATION_BUCKETS:
            lines.append(
                "techyz_http_request_duration_seconds_bucket"
                f'{{le="{upper_bound:g}"}} {duration_buckets[upper_bound]}'
            )
        lines.extend([
            "techyz_http_request_duration_seconds_bucket{le=\"+Inf\"} "
            f"{duration_count}",
            f"techyz_http_request_duration_seconds_sum {duration_sum:.6f}",
            f"techyz_http_request_duration_seconds_count {duration_count}",
        ])

        if self._latest_cpu_pct is not None:
            lines.extend([
                "",
                "# HELP techyz_host_cpu_percent Host CPU utilization percent",
                "# TYPE techyz_host_cpu_percent gauge",
                f"techyz_host_cpu_percent {self._latest_cpu_pct}",
            ])
        if self._latest_ram_used_gb is not None:
            lines.extend([
                "",
                "# HELP techyz_host_memory_used_gb Host RAM used in gigabytes",
                "# TYPE techyz_host_memory_used_gb gauge",
                f"techyz_host_memory_used_gb {self._latest_ram_used_gb}",
            ])

        # GPU Metrics: separating VRAM memory bytes and utilization from compute processor utilization
        if self._latest_gpu_memory_used_bytes is not None:
            lines.extend([
                "",
                "# HELP techyz_gpu_memory_used_bytes GPU VRAM memory used in bytes",
                "# TYPE techyz_gpu_memory_used_bytes gauge",
                f"techyz_gpu_memory_used_bytes {self._latest_gpu_memory_used_bytes}",
            ])
        if self._latest_gpu_memory_total_bytes is not None:
            lines.extend([
                "",
                "# HELP techyz_gpu_memory_total_bytes GPU VRAM total memory in bytes",
                "# TYPE techyz_gpu_memory_total_bytes gauge",
                f"techyz_gpu_memory_total_bytes {self._latest_gpu_memory_total_bytes}",
            ])
        if self._latest_gpu_memory_utilization_percent is not None:
            lines.extend([
                "",
                "# HELP techyz_gpu_memory_utilization_percent GPU VRAM memory utilization percent",
                "# TYPE techyz_gpu_memory_utilization_percent gauge",
                f"techyz_gpu_memory_utilization_percent {self._latest_gpu_memory_utilization_percent}",
            ])
        if self._latest_gpu_compute_utilization_percent is not None:
            lines.extend([
                "",
                "# HELP techyz_gpu_compute_utilization_percent GPU compute processor utilization percent",
                "# TYPE techyz_gpu_compute_utilization_percent gauge",
                f"techyz_gpu_compute_utilization_percent {self._latest_gpu_compute_utilization_percent}",
            ])
        # Deprecated legacy alias for backward compatibility
        if self._latest_gpu_pct is not None:
            lines.extend([
                "",
                "# HELP techyz_gpu_utilization_percent Deprecated alias for GPU VRAM memory utilization percent",
                "# TYPE techyz_gpu_utilization_percent gauge",
                f"techyz_gpu_utilization_percent {self._latest_gpu_pct}",
            ])

        return "\n".join(lines) + "\n"

    @staticmethod
    def _concurrency_lease_seconds() -> float:
        try:
            from core.config import settings

            return float(settings.observatory_concurrency_lease_seconds)
        except Exception:
            return 600.0


# Singleton instance
_GLOBAL_BUFFER: Optional[ObservatoryBuffer] = None
_BUFFER_LOCK = threading.Lock()


def get_observatory_buffer() -> ObservatoryBuffer:
    global _GLOBAL_BUFFER
    with _BUFFER_LOCK:
        if _GLOBAL_BUFFER is None:
            _GLOBAL_BUFFER = ObservatoryBuffer()
        return _GLOBAL_BUFFER
