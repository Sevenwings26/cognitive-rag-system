# modules/rag_core/orchestrator/strategies/sql_strategy.py
import re
import logging
from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy.orm import Session
from qdrant_client.models import Filter, FieldCondition, MatchValue

from core.crypto import decrypt_connection_config
from modules.auth.domain.tokens import TokenData
from modules.governance.domain.models import IngestionJob
from modules.governance.services.audit_logger import AuditLogger
from modules.connectors.security.sql_guard import SQLSecurityGuard
from modules.rag_core.tools.sql_agent import DynamicSQLAgent
from modules.rag_core.providers.llm import BaseLLMService
from modules.rag_core.retrieval.vector_store import VectorStoreService
from modules.rag_core.orchestrator.strategies.base import BaseRetrievalStrategy

logger = logging.getLogger("sql_strategy")

class DynamicSQLStrategy(BaseRetrievalStrategy):
    """
    Route B: Dynamic Text-to-SQL Execution Strategy.
    Resolves multi-database connector targets for the tenant, matches schema DDL context,
    synthesizes read-only SQL queries via DynamicSQLAgent, executes within an engine-level
    read-only transaction sandbox, and formats markdown tables and natural language summaries.
    """
    SUPPORTED_SOURCE_TYPES = [
        "POSTGRES_DB", "POSTGRESQL", "POSTGRES",
        "MYSQL_DB", "MYSQL", "MARIADB",
        "ORACLE_DB", "ORACLE",
        "MSSQL_DB", "MSSQL", "SQLSERVER"
    ]

    # Semantic database mapping for banking / enterprise domains
    DOMAIN_TABLE_KEYWORDS = {
        "noros_customer_db": ["customer", "bvn", "kyc", "identity", "nin", "address", "segment"],
        "noros_core_banking_db": ["account", "balance", "standing_order", "beneficiar"],
        "noros_lending_db": ["loan", "lending", "repayment", "credit", "collateral", "installment", "amortization"],
        "noros_payments_db": ["payment", "transfer", "bill", "card", "pos", "merchant"],
        "noros_compliance_db": ["compliance", "aml", "sanction", "fraud", "suspicious"],
        "noros_operations_db": ["operation", "branch", "atm", "employee", "relationship_manager"]
    }

    def __init__(self, vector_store: VectorStoreService, llm_service: BaseLLMService):
        self.vector_store = vector_store
        self.llm = llm_service

    def _resolve_target_job(
        self,
        query: str,
        db_jobs: List[IngestionJob],
        schema_chunks: List[Any]
    ) -> Optional[IngestionJob]:
        """
        Dynamically determines which registered database contains the relevant tables.
        Uses matched schema chunk comments/filenames first, then domain table keywords.
        """
        if not db_jobs:
            return None
        if len(db_jobs) == 1:
            return db_jobs[0]

        job_map = {j.name.lower(): j for j in db_jobs}
        lower_query = query.lower()

        # 1. Inspect schema chunks for database name header (e.g. "-- Database: noros_customer_db")
        for chunk in schema_chunks:
            content = getattr(chunk, "payload", {}).get("content", "")
            filename = getattr(chunk, "payload", {}).get("filename", "")
            for db_name, job in job_map.items():
                if db_name in content.lower() or db_name in filename.lower():
                    logger.info(f"[DYNAMIC SQL] Matched target database '{job.name}' via schema chunk.")
                    return job

        # 2. Match based on domain table keywords
        for db_name, keywords in self.DOMAIN_TABLE_KEYWORDS.items():
            if any(k in lower_query for k in keywords):
                if db_name in job_map:
                    logger.info(f"[DYNAMIC SQL] Matched target database '{db_name}' via query keyword matching.")
                    return job_map[db_name]

        # 3. Default fallback to first job
        return db_jobs[0]

    def _get_schema_context(
        self,
        query: str,
        user_context: TokenData,
        target_job: IngestionJob,
        dialect: str,
        db_url: str,
        db: Optional[Session] = None
    ) -> Tuple[str, List[Any]]:
        """
        Retrieves schema DDL context directly from relational DocumentChunks for the target job,
        with Qdrant semantic vector search and dynamic reflection fallbacks.
        """
        from modules.governance.domain.models import EnterpriseDocument

        # 1. Primary: Load complete table DDLs directly for this target database job
        if db:
            docs = db.query(EnterpriseDocument).filter(
                EnterpriseDocument.org_id == user_context.org_id,
                EnterpriseDocument.job_id == target_job.id
            ).all()
            if docs:
                ddls = [c.content for d in docs for c in d.chunks if c.content]
                if ddls:
                    logger.info(f"[DYNAMIC SQL] Loaded {len(ddls)} complete table DDLs from DB for target '{target_job.name}'.")
                    return "\n\n".join(ddls), docs

        # 2. Secondary: Search Qdrant for matching schema chunks
        schema_filter = Filter(
            must=[
                FieldCondition(key="org_id", match=MatchValue(value=user_context.org_id))
            ]
        )
        query_vector = self.llm.get_embeddings(query)
        schema_hits = self.vector_store.search_vectors(
            query_vector=query_vector,
            search_filter=schema_filter,
            limit=8,
            score_threshold=0.20
        )

        matched_ddls = []
        schema_chunks = []
        for hit in schema_hits:
            payload = hit.get("payload", {})
            filename = payload.get("filename", "")
            content = payload.get("content", "")
            if filename.startswith("schema_") or "CREATE TABLE" in content or "Database Schema DDL" in content:
                if target_job.name.lower() in content.lower() or target_job.name.lower() in filename.lower():
                    matched_ddls.append(content)
                    schema_chunks.append(hit)
                elif not matched_ddls:
                    matched_ddls.append(content)
                    schema_chunks.append(hit)

        if matched_ddls:
            return "\n\n".join(matched_ddls[:5]), schema_chunks

        # 3. Dynamic Schema Reflection Fallback
        try:
            from modules.connectors.sources.databases.schema_reflector import DatabaseSchemaReflector
            decrypted = decrypt_connection_config(target_job.connection_config)
            reflected = DatabaseSchemaReflector.reflect_schema(
                dialect=dialect,
                db_url=db_url,
                target_schema=decrypted.get("schema")
            )
            return "\n\n".join([t["ddl"] for t in reflected[:10]]), []
        except Exception as ref_err:
            logger.warning(f"[DYNAMIC SQL] Dynamic schema reflection failed: {ref_err}")
            return "", []

    def execute(
        self,
        query: str,
        user_context: TokenData,
        db: Optional[Session] = None,
        session_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        template_id: Optional[str] = None,
        top_k: int = 3,
        score_threshold: float = 0.35,
        mode: str = "auto",
        **kwargs
    ) -> Optional[Tuple[str, List[Dict[str, Any]], bool, float]]:
        if not db:
            return None

        org_name = kwargs.get("org_name", "Enterprise")

        try:
            db_jobs = db.query(IngestionJob).filter(
                IngestionJob.org_id == user_context.org_id,
                IngestionJob.source_type.in_(self.SUPPORTED_SOURCE_TYPES)
            ).all()

            if not db_jobs:
                logger.info("[DYNAMIC SQL] No database connector jobs configured for org.")
                return None

            # 1. Resolve Target Database Connector
            # Quick vector probe to discover relevant schema chunks
            query_vector = self.llm.get_embeddings(query)
            probe_hits = self.vector_store.search_vectors(
                query_vector=query_vector,
                search_filter=Filter(must=[FieldCondition(key="org_id", match=MatchValue(value=user_context.org_id))]),
                limit=5,
                score_threshold=0.20
            )

            job = self._resolve_target_job(query, db_jobs, probe_hits)
            if not job:
                return None

            decrypted_config = decrypt_connection_config(job.connection_config)
            dialect = SQLSecurityGuard.canonical_dialect(job.source_type)
            db_url = SQLSecurityGuard.safe_build_db_url(dialect, decrypted_config)

            # 2. Get Schema Context (DDLs)
            schema_context, _ = self._get_schema_context(query, user_context, job, dialect, db_url, db=db)
            if not schema_context.strip():
                logger.warning(f"[DYNAMIC SQL] No schema context available for target database '{job.name}'.")
                return None

            logger.info(f"[DYNAMIC SQL] Generating query for target DB='{job.name}' ({dialect})...")

            # 3. Dynamic SQL Agent Generation & Read-Only Execution
            sql_result = DynamicSQLAgent.generate_and_execute_sql(
                user_query=query,
                schema_context=schema_context,
                dialect=dialect,
                db_url=db_url,
                llm_service=self.llm
            )

            if sql_result.get("status") == "success":
                rows = sql_result.get("rows", [])
                cols = sql_result.get("columns", [])
                executed_sql = sql_result.get("sql", "")

                if rows:
                    md_table_lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
                    for r in rows[:15]:
                        row_vals = [str(r.get(c, "N/A")) for c in cols]
                        md_table_lines.append("| " + " | ".join(row_vals) + " |")
                    table_summary = f"\n\n**Database Query Results:**\n\n" + "\n".join(md_table_lines)
                else:
                    table_summary = "\n\n*The database query returned 0 matching records.*"

                synthesis_prompt = (
                    f"User Inquiry: {query}\n"
                    f"Executed SQL: {executed_sql}\n"
                    f"Result Data:\n{table_summary}\n\n"
                    f"Provide a clear, direct, and professional answer to the user's inquiry based on this query result. "
                    f"State the exact customer name, identifier, or values retrieved."
                )
                answer = self.llm.generate_text(
                    synthesis_prompt,
                    system_instruction=f"You are an enterprise data analyst assistant for {org_name}."
                )
                full_answer = f"{answer}\n{table_summary}"

                sources = [{
                    "source_name": f"{job.name} ({dialect.upper()} Database)",
                    "filename": f"{job.name} ({dialect.upper()})",
                    "chunk_id": "sql_execution",
                    "sql_query": executed_sql,
                    "row_count": len(rows),
                    "relevance_score": 0.95,
                    "preview": f"SQL: {executed_sql}"
                }]

                AuditLogger.log(
                    db=db,
                    org_id=user_context.org_id,
                    user_id=user_context.user_id,
                    action="DYNAMIC_SQL_QUERY",
                    resource_type="DATABASE",
                    resource_id=job.id,
                    details={"sql": executed_sql, "rows_returned": len(rows), "database": job.name}
                )

                return full_answer, sources, True, 0.95
            else:
                logger.warning(f"[DYNAMIC SQL] Execution failed: {sql_result.get('error')}. Falling back.")
                return None
        except Exception as e:
            logger.warning(f"[DYNAMIC SQL] Error: {e}. Falling back.")
            return None
