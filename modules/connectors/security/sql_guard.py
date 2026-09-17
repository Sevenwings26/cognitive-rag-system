# modules/connectors/security/sql_guard.py
"""
SQL Security Guard & Anti-Write Defense Subsystem.
Enforces multi-layered AST query validation, SSRF target validation,
credential masking, and engine-native read-only transactions.
"""
from __future__ import annotations

import re
import socket
import logging
import ipaddress
from typing import Dict, Any, Generator, Optional, Tuple, Iterable, Sequence
from urllib.parse import parse_qsl, quote, quote_plus, unquote, urlencode, urlsplit, urlunsplit

import sqlparse
from sqlparse.tokens import Comment, DML, Keyword, Literal, Name
from sqlalchemy import text
from sqlalchemy.engine import Engine

from core.config import settings

logger = logging.getLogger("sql_security_guard")


class InsecureQueryError(ValueError):
    """Raised when an SQL query violates the zero-write read-only security policy."""


class DatabaseSecurityTargetError(ValueError):
    """Raised when a database connection target violates SSRF or network boundary rules."""


SUPPORTED_DB_SCHEMES = {
    "postgresql": {"postgres", "postgresql", "postgresql+psycopg2", "postgresql+psycopg"},
    "mysql": {"mysql", "mysql+pymysql", "mysql+mysqldb"},
    "oracle": {"oracle", "oracle+oracledb", "oracle+cx_oracle"},
    "mssql": {"mssql", "mssql+pyodbc", "mssql+pymssql"},
    "sqlite": {"sqlite", "sqlite3"},
}

_DIALECT_ALIASES = {
    "postgres": "postgresql",
    "postgresql": "postgresql",
    "postgres_db": "postgresql",
    "postgresql_db": "postgresql",
    "mysql": "mysql",
    "mysql_db": "mysql",
    "mariadb": "mysql",
    "oracle": "oracle",
    "oracle_db": "oracle",
    "mssql": "mssql",
    "mssql_db": "mssql",
    "sqlserver": "mssql",
    "sqlite": "sqlite",
}

_FORBIDDEN_KEYWORDS = {
    "ALTER", "CALL", "COPY", "CREATE", "DELETE", "DO", "DROP", "EXEC",
    "EXECUTE", "GRANT", "INSERT", "INTO", "LOAD", "MERGE", "REPLACE",
    "REVOKE", "TRUNCATE", "UPDATE", "VACUUM", "ATTACH", "DETACH",
    "PRAGMA", "REINDEX", "SHUTDOWN",
}

_SIDE_EFFECTING_FUNCTIONS = {
    # PostgreSQL administrative, delay, file, locking, and cross-DB functions
    "copy", "dblink", "dblink_connect", "dblink_exec", "lo_export", "lo_import",
    "nextval", "pg_advisory_lock", "pg_advisory_lock_shared",
    "pg_advisory_xact_lock", "pg_advisory_xact_lock_shared", "pg_cancel_backend",
    "pg_create_restore_point", "pg_logical_emit_message", "pg_promote",
    "pg_notify", "pg_read_binary_file", "pg_read_file", "pg_reload_conf",
    "pg_rotate_logfile", "pg_sleep", "pg_switch_wal",
    "pg_terminate_backend", "pg_wal_replay_pause", "pg_wal_replay_resume",
    "set_config", "setval",
    # MySQL functions that delay, lock, read server files, or create load
    "benchmark", "get_lock", "load_file", "master_pos_wait", "release_lock",
    "sleep", "sys_exec",
    # Oracle packages with file, network, scheduler, lock, or pipe side effects
    "dbms_lock.sleep", "dbms_pipe.send_message", "dbms_scheduler.run_job",
    "utl_file.fopen", "utl_http.request",
    # SQL Server functions/procedures that execute commands, delay, or read files
    "xp_cmdshell", "sp_executesql", "openrowset", "opendatasource", "openquery",
    "bulk_insert", "waitfor",
}

_SENSITIVE_QUERY_KEYS = {
    "access_token", "apikey", "api_key", "auth", "bearer",
    "client_secret", "key", "pass", "password", "pwd", "secret", "token",
}

_HARD_BLOCKED_IPS = {
    ipaddress.ip_address("169.254.169.254"),  # AWS/Azure/OpenStack metadata
    ipaddress.ip_address("169.254.170.2"),    # AWS container credentials
    ipaddress.ip_address("100.100.100.200"),  # Alibaba metadata
    ipaddress.ip_address("fd00:ec2::254"),    # AWS IPv6 metadata
}

_HARD_BLOCKED_HOSTS = {
    "metadata.google.internal",
    "metadata.azure.internal",
}


class SQLSecurityGuard:
    """
    Unified Security Guardrail for Enterprise Database Ingestion & Text-to-SQL.
    Provides multi-layered defense-in-depth:
      Layer 1: AST / Token SQL Validation (Anti-Write, Anti-SQLi, Anti-Exfiltration)
      Layer 2: SSRF & Network Boundary Validation
      Layer 3: Credential Masking & Safe URL Encoding
      Layer 4: Engine-Native Read-Only Execution & Timeouts
    """

    @staticmethod
    def canonical_dialect(dialect: str) -> str:
        canonical = _DIALECT_ALIASES.get((dialect or "").lower().strip())
        if canonical is None:
            raise ValueError(
                f"Unsupported database dialect '{dialect}'. "
                f"Allowed dialects: {', '.join(sorted(_DIALECT_ALIASES.keys()))}"
            )
        return canonical

    @classmethod
    def validate_query(cls, sql_query: str, dialect: str = "postgresql") -> str:
        """
        Validates that an SQL query is strictly a single, read-only SELECT statement.
        Rejects DDL, DML, multi-statement injection, side-effecting functions,
        linked server escapes, and out-of-band file exports.
        Returns the stripped, sanitized query string or raises InsecureQueryError.
        """
        if not sql_query or not sql_query.strip():
            raise InsecureQueryError("SQL query cannot be empty.")

        cleaned_sql = sql_query.strip().rstrip(";")

        # 1. Single Statement AST Verification
        statements = [stmt for stmt in sqlparse.parse(cleaned_sql) if str(stmt).strip()]
        if len(statements) != 1:
            raise InsecureQueryError(
                f"Security Violation: Only a single SQL statement is allowed (detected {len(statements)})."
            )

        statement = statements[0]
        statement_type = statement.get_type().upper()

        # SELECT or UNKNOWN (which covers CTEs like WITH ... SELECT)
        if statement_type not in ("SELECT", "UNKNOWN"):
            raise InsecureQueryError(
                f"Security Violation: Disallowed statement type '{statement_type}'. Only SELECT queries are permitted."
            )

        # 2. Token-level forbidden mutation keywords check
        for token in statement.flatten():
            normalized = token.value.upper().strip()
            if normalized in _FORBIDDEN_KEYWORDS:
                raise InsecureQueryError(
                    f"Security Violation: Forbidden SQL keyword detected: '{normalized}'."
                )

        # 3. Pattern / Function Level Side-Effect Inspection
        normalized_sql = re.sub(r"\s+", " ", str(statement)).lower()

        for func in _SIDE_EFFECTING_FUNCTIONS:
            # Word boundary followed by optional whitespace and opening parenthesis
            if re.search(rf"(?<![a-z0-9_]){re.escape(func)}\s*\(", normalized_sql):
                raise InsecureQueryError(
                    f"Security Violation: Side-effecting SQL function or procedure is not allowed: '{func}()'."
                )

        # 4. Check for dblink exfiltration
        if re.search(r"(?<![a-z0-9_])dblink[a-z0-9_]*\s*\(", normalized_sql):
            raise InsecureQueryError("Security Violation: dblink cross-database functions are not allowed.")

        # 5. Check for MySQL INTO OUTFILE / DUMPFILE
        if re.search(r"\binto\s+(?:out|dump)file\b", normalized_sql):
            raise InsecureQueryError("Security Violation: File exfiltration (INTO OUTFILE/DUMPFILE) is forbidden.")

        # 6. Check for double-dot notation and linked servers (4-part identifiers)
        if ".." in normalized_sql:
            raise InsecureQueryError("Security Violation: Double-dot or linked-server notations are forbidden.")

        four_part_regex = (
            r'(?:"[^"]+"|[a-z0-9_]+|\[[^\]]+\])\.'
            r'(?:"[^"]+"|[a-z0-9_]+|\[[^\]]+\])\.'
            r'(?:"[^"]+"|[a-z0-9_]+|\[[^\]]+\])\.'
            r'(?:"[^"]+"|[a-z0-9_]+|\[[^\]]+\])'
        )
        if re.search(four_part_regex, normalized_sql):
            raise InsecureQueryError("Security Violation: Four-part linked server identifiers are forbidden.")

        # 7. Check for sequence mutations
        if re.search(r"\bnext\s+value\s+for\b", normalized_sql):
            raise InsecureQueryError("Security Violation: Sequence state mutations (NEXT VALUE FOR) are forbidden.")

        return cleaned_sql

    @classmethod
    def _csv_values(cls, raw: str) -> set[str]:
        return {item.strip().rstrip(".").lower() for item in (raw or "").split(",") if item.strip()}

    @classmethod
    def _allowed_networks(cls, raw: str) -> Iterable[ipaddress.IPv4Network | ipaddress.IPv6Network]:
        for item in cls._csv_values(raw):
            try:
                yield ipaddress.ip_network(item, strict=True)
            except ValueError as exc:
                logger.warning(f"Invalid CIDR in configuration: {item!r}")

    @classmethod
    def validate_db_target(cls, dialect: str, db_url: str) -> Tuple[str, Tuple[str, ...]]:
        """
        Validates target hostname/IP against SSRF, cloud metadata, and unauthorized private network access.
        Fails closed on DNS errors or unallowlisted private addresses.
        """
        canonical = cls.canonical_dialect(dialect)
        if canonical == "sqlite":
            return ("localhost", ("127.0.0.1",))
        parsed = urlsplit(db_url)
        host = (parsed.hostname or "").rstrip(".").lower()

        if not host:
            raise DatabaseSecurityTargetError("Database URL must include a resolvable host.")

        if host in _HARD_BLOCKED_HOSTS:
            raise DatabaseSecurityTargetError(f"Cloud metadata host '{host}' is forbidden.")

        allowed_hosts = cls._csv_values(settings.ALLOWED_DB_PRIVATE_HOSTS)
        allowed_networks = tuple(cls._allowed_networks(settings.ALLOWED_DB_PRIVATE_CIDRS))

        try:
            resolved = {
                ipaddress.ip_address(sockaddr[0])
                for _family, _kind, _proto, _canonname, sockaddr in socket.getaddrinfo(
                    host, None, type=socket.SOCK_STREAM
                )
            }
        except (OSError, ValueError) as exc:
            raise DatabaseSecurityTargetError(f"Database host '{host}' could not be resolved.") from exc

        if not resolved:
            raise DatabaseSecurityTargetError(f"Database host '{host}' resolved to no IP addresses.")

        for ip in resolved:
            if ip in _HARD_BLOCKED_IPS or ip.is_link_local or ip.is_unspecified or ip.is_multicast:
                raise DatabaseSecurityTargetError(f"Database target IP '{ip}' is a restricted address.")

            restricted = ip.is_private or ip.is_loopback or getattr(ip, "is_site_local", False)
            explicitly_allowed = (host in allowed_hosts) or any(ip in net for net in allowed_networks)

            if restricted and not explicitly_allowed:
                raise DatabaseSecurityTargetError(
                    f"Security Block: Database host '{host}' resolves to restricted/private IP '{ip}'. "
                    "If this is an internal database, add it to ALLOWED_DB_PRIVATE_HOSTS."
                )

        return canonical, tuple(sorted(str(ip) for ip in resolved))

    @classmethod
    def safe_build_db_url(cls, dialect: str, config: Dict[str, Any]) -> str:
        """
        Builds a safe, normalized SQLAlchemy connection URL with properly URL-encoded credentials.
        Appends engine-appropriate read-only routing flags where applicable.
        """
        canonical = cls.canonical_dialect(dialect)

        if "db_url" in config and config["db_url"]:
            raw_url = str(config["db_url"]).strip()
            # Normalize schemes
            if canonical == "postgresql":
                if raw_url.startswith("postgres://"):
                    raw_url = raw_url.replace("postgres://", "postgresql+psycopg2://", 1)
                elif raw_url.startswith("postgresql://") and not raw_url.startswith("postgresql+psycopg2://"):
                    raw_url = raw_url.replace("postgresql://", "postgresql+psycopg2://", 1)
            elif canonical == "mysql":
                if raw_url.startswith("mysql://"):
                    raw_url = raw_url.replace("mysql://", "mysql+pymysql://", 1)
            elif canonical == "oracle":
                if raw_url.startswith("oracle://"):
                    raw_url = raw_url.replace("oracle://", "oracle+oracledb://", 1)
            elif canonical == "mssql":
                if "applicationintent=" not in raw_url.lower():
                    sep = "&" if "?" in raw_url else "?"
                    raw_url = f"{raw_url}{sep}ApplicationIntent=ReadOnly"
            return raw_url

        # Build from structured fields with URL encoding
        user = quote_plus(str(config.get("username", "")))
        pwd = quote_plus(str(config.get("password", "")))
        host = str(config.get("host", "localhost"))
        auth_part = f"{user}:{pwd}@" if user or pwd else ""

        if canonical == "postgresql":
            port = int(config.get("port", 5432))
            db = str(config.get("database", "postgres"))
            return f"postgresql+psycopg2://{auth_part}{host}:{port}/{db}"

        elif canonical == "mysql":
            port = int(config.get("port", 3306))
            db = str(config.get("database", "mysql"))
            return f"mysql+pymysql://{auth_part}{host}:{port}/{db}"

        elif canonical == "oracle":
            port = int(config.get("port", 1521))
            service = str(config.get("service_name", "ORCLPDB1"))
            return f"oracle+oracledb://{auth_part}{host}:{port}/?service_name={service}"

        elif canonical == "mssql":
            port = int(config.get("port", 1433))
            db = str(config.get("database", "master"))
            try:
                import pyodbc
                driver = quote_plus("ODBC Driver 18 for SQL Server")
                return (
                    f"mssql+pyodbc://{auth_part}{host}:{port}/{db}?"
                    f"driver={driver}&TrustServerCertificate=yes&ApplicationIntent=ReadOnly"
                )
            except ImportError:
                return f"mssql+pymssql://{auth_part}{host}:{port}/{db}"

        raise ValueError(f"Unsupported dialect: {dialect}")

    @classmethod
    def mask_db_url(cls, url: str) -> str:
        """Returns a structurally masked URL without exposing passwords or sensitive query keys."""
        if not url:
            return ""
        try:
            parsed = urlsplit(url)
            hostname = parsed.hostname or ""
            if ":" in hostname and not hostname.startswith("["):
                hostname = f"[{hostname}]"
            port = f":{parsed.port}" if parsed.port is not None else ""
            if parsed.username is not None:
                username = quote(unquote(parsed.username), safe="")
                netloc = f"{username}:***@{hostname}{port}"
            else:
                netloc = f"{hostname}{port}"

            masked_query = []
            for key, value in parse_qsl(parsed.query, keep_blank_values=True):
                lowered = key.lower().replace("-", "_")
                if lowered in _SENSITIVE_QUERY_KEYS or any(m in lowered for m in ("password", "secret", "token", "pwd")):
                    value = "***"
                masked_query.append((key, value))

            return urlunsplit((parsed.scheme, netloc, parsed.path, urlencode(masked_query), ""))
        except Exception:
            scheme = url.split(":", 1)[0] if ":" in url else "db"
            return f"{scheme}://***"

    # Backward-compatible alias
    build_connection_url = safe_build_db_url

    @classmethod
    def execute_read_only_query(
        cls,
        engine: Engine,
        dialect: str,
        sql_query: str,
        timeout_ms: Optional[int] = None,
        batch_size: Optional[int] = None
    ) -> Generator[Dict[str, Any], None, None]:
        """
        Executes a validated query in an engine-enforced READ-ONLY transaction sandbox.
        Streams rows in batches to prevent memory exhaustion.
        """
        canonical = cls.canonical_dialect(dialect)
        timeout = int(timeout_ms or settings.EXTERNAL_DB_STATEMENT_TIMEOUT_MS)
        fetch_size = int(batch_size or settings.EXTERNAL_DB_FETCH_BATCH_SIZE)

        # Pre-execution validation
        validated_sql = cls.validate_query(sql_query, dialect=canonical)

        with engine.connect() as conn:
            if canonical == "postgresql":
                with conn.begin():
                    conn.exec_driver_sql("SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY")
                    conn.exec_driver_sql(f"SET LOCAL statement_timeout = {timeout}")
                    result = conn.execute(text(validated_sql))
                    while True:
                        rows = result.mappings().fetchmany(fetch_size)
                        if not rows:
                            break
                        for row in rows:
                            yield dict(row)

            elif canonical == "mysql":
                conn.exec_driver_sql("SET SESSION TRANSACTION READ ONLY")
                conn.exec_driver_sql(f"SET SESSION MAX_EXECUTION_TIME = {timeout}")
                conn.commit()
                with conn.begin():
                    result = conn.execute(text(validated_sql))
                    while True:
                        rows = result.mappings().fetchmany(fetch_size)
                        if not rows:
                            break
                        for row in rows:
                            yield dict(row)

            elif canonical == "oracle":
                driver_conn = getattr(conn.connection, "driver_connection", None)
                if driver_conn and hasattr(driver_conn, "call_timeout"):
                    driver_conn.call_timeout = timeout

                with conn.begin():
                    conn.exec_driver_sql("SET TRANSACTION READ ONLY")
                    result = conn.execute(text(validated_sql))
                    while True:
                        rows = result.mappings().fetchmany(fetch_size)
                        if not rows:
                            break
                        for row in rows:
                            yield dict(row)

            elif canonical == "mssql":
                with conn.begin():
                    # Check database updateability
                    updateability = conn.execute(text(
                        "SELECT CAST(DATABASEPROPERTYEX(DB_NAME(), 'Updateability') AS VARCHAR)"
                    )).scalar()
                    if updateability and str(updateability).upper() != "READ_ONLY":
                        logger.warning(
                            f"MSSQL database '{engine.url.database}' is not physically READ_ONLY. "
                            "Relying on AST query guard and ApplicationIntent=ReadOnly."
                        )

                    conn.exec_driver_sql(f"SET LOCK_TIMEOUT {timeout}")
                    result = conn.execute(text(validated_sql))
                    while True:
                        rows = result.mappings().fetchmany(fetch_size)
                        if not rows:
                            break
                        for row in rows:
                            yield dict(row)
            else:
                with conn.begin():
                    result = conn.execute(text(validated_sql))
                    while True:
                        rows = result.mappings().fetchmany(fetch_size)
                        if not rows:
                            break
                        for row in rows:
                            yield dict(row)
