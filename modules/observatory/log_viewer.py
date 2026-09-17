"""Bounded, redacted access to LocalMind application logs for super admins."""

from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Iterable

from core.config import settings
from core.logging import get_recent_logs


_LOG_LINE = re.compile(
    r"^\[(?P<time>\d{2}:\d{2}:\d{2}\.\d{3})\s+"
    r"(?P<level>DEBUG|INFO|WARNING|ERROR|CRITICAL)\s+"
    r"(?P<logger>\S+)\s+user=(?P<user>[^\]]*)\]\s*(?P<message>.*)$"
)
_LEVELS = {"DEBUG": 10, "INFO": 20, "WARNING": 30, "ERROR": 40, "CRITICAL": 50}
_SECRET_ASSIGNMENT = re.compile(
    r"(?i)\b(password|passwd|pwd|secret|client_secret|api[_-]?key|access[_-]?token|"
    r"refresh[_-]?token|token|authorization|cookie|aws[_-]?access[_-]?key[_-]?id|"
    r"aws[_-]?secret[_-]?access[_-]?key)\b(\s*[:=]\s*)([^\s,;]+)"
)
_BEARER = re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._~+\-/]+=*")
_URL_CREDENTIALS = re.compile(r"(?P<scheme>[a-zA-Z][a-zA-Z0-9+.-]*://)[^\s/@:]+:[^\s/@]+@")
_CONTROL_CHARS = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")
_MAX_SCAN_BYTES = 4 * 1024 * 1024
_MAX_STREAM_BYTES = 1024 * 1024


def redact_log_text(value: object, *, max_length: int = 8_000) -> str:
    """Remove common credentials and unsafe control characters at read time."""
    text = _CONTROL_CHARS.sub(" ", str(value or ""))
    text = _BEARER.sub("Bearer [REDACTED]", text)
    text = _URL_CREDENTIALS.sub(r"\g<scheme>[REDACTED]@", text)
    text = _SECRET_ASSIGNMENT.sub(r"\1\2[REDACTED]", text)
    return text[:max_length]


def _matches(entry: dict, *, level: str | None, logger_name: str | None, search: str | None) -> bool:
    if level and _LEVELS.get(str(entry.get("level", "")).upper(), 0) < _LEVELS[level]:
        return False
    if logger_name and logger_name.casefold() not in str(entry.get("logger", "")).casefold():
        return False
    if search:
        haystack = " ".join(
            str(entry.get(key, "")) for key in ("level", "logger", "user_email", "message")
        ).casefold()
        if search.casefold() not in haystack:
            return False
    return True


def _safe_entry(entry: dict, source: str) -> dict:
    timestamp = entry.get("timestamp")
    if isinstance(timestamp, datetime):
        timestamp = timestamp.isoformat(timespec="milliseconds")
    return {
        "timestamp": str(timestamp or ""),
        "level": str(entry.get("level", "INFO")).upper(),
        "logger": redact_log_text(entry.get("logger", ""), max_length=256),
        "user_email": redact_log_text(entry.get("user_email", ""), max_length=320),
        "message": redact_log_text(entry.get("raw_message", entry.get("message", ""))),
        "source": source,
    }


def _log_files() -> list[Path]:
    log_dir = (settings.data_dir / "logs").resolve()
    if not log_dir.is_dir():
        return []
    # The fixed glob and resolved-parent check prevent caller-controlled paths.
    paths = [p.resolve() for p in log_dir.glob("localmind.log*") if p.is_file()]
    paths = [p for p in paths if p.parent == log_dir and re.fullmatch(r"localmind\.log(?:\.\d{4}-\d{2}-\d{2})?", p.name)]
    return sorted(paths, key=lambda path: path.stat().st_mtime, reverse=True)


def _bounded_lines(paths: Iterable[Path], *, max_bytes: int = _MAX_SCAN_BYTES) -> list[tuple[Path, str]]:
    remaining = max_bytes
    result: list[tuple[Path, str]] = []
    for path in paths:
        if remaining <= 0:
            break
        size = min(path.stat().st_size, remaining)
        if size <= 0:
            continue
        with path.open("rb") as handle:
            handle.seek(max(0, path.stat().st_size - size))
            blob = handle.read(size)
        remaining -= len(blob)
        text = blob.decode("utf-8", errors="replace")
        lines = text.splitlines()
        if path.stat().st_size > size and lines:
            lines = lines[1:]
        # Inputs are newest-first so the byte budget always favours recent
        # diagnostics. Prepending each selected file restores chronological
        # order without reversing traceback continuation lines.
        result = [(path, line) for line in lines] + result
    return result


def _persistent_entries(since_minutes: int, *, current_only: bool = False) -> list[dict]:
    cutoff = datetime.now() - timedelta(minutes=since_minutes)
    entries: list[dict] = []
    current: dict | None = None
    paths = _log_files()
    if current_only:
        paths = [path for path in paths if path.name == "localmind.log"]
    for path, line in _bounded_lines(
        paths,
        max_bytes=_MAX_STREAM_BYTES if current_only else _MAX_SCAN_BYTES,
    ):
        match = _LOG_LINE.match(line)
        if match:
            file_day = datetime.fromtimestamp(path.stat().st_mtime).date()
            suffix_match = re.search(r"(\d{4}-\d{2}-\d{2})$", path.name)
            if suffix_match:
                file_day = datetime.strptime(suffix_match.group(1), "%Y-%m-%d").date()
            timestamp = datetime.combine(file_day, datetime.strptime(match.group("time"), "%H:%M:%S.%f").time())
            current = {
                "timestamp": timestamp,
                "level": match.group("level"),
                "logger": match.group("logger"),
                "user_email": match.group("user"),
                "message": match.group("message"),
            }
            entries.append(current)
        elif current is not None:
            current["message"] = f'{current["message"]}\n{line}'
    return [entry for entry in entries if entry["timestamp"] >= cutoff]


def query_logs(
    *,
    source: str,
    level: str | None,
    logger_name: str | None,
    search: str | None,
    since_minutes: int,
    offset: int,
    limit: int,
) -> dict:
    if source == "live":
        entries = get_recent_logs(since=timedelta(minutes=since_minutes))
    else:
        entries = _persistent_entries(since_minutes, current_only=source == "stream")

    safe_entries = [_safe_entry(entry, source) for entry in entries]
    filtered = [
        entry for entry in safe_entries
        if _matches(entry, level=level, logger_name=logger_name, search=search)
    ]
    filtered.sort(key=lambda entry: entry["timestamp"], reverse=True)
    page = filtered[offset:offset + limit]
    counts = {name.lower(): 0 for name in _LEVELS}
    for entry in filtered:
        key = entry["level"].lower()
        if key in counts:
            counts[key] += 1
    return {
        "entries": page,
        "total": len(filtered),
        "offset": offset,
        "limit": limit,
        "has_more": offset + len(page) < len(filtered),
        "counts": counts,
        "source": source,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "coverage": (
            "Current API process since startup (up to 500 entries)."
            if source == "live"
            else (
                "Current shared LocalMind application log; external service logs are not included."
                if source == "stream"
                else "Bounded LocalMind application log files; external service logs are not included."
            )
        ),
    }
