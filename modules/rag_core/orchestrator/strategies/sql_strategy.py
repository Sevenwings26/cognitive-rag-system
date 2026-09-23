# modules/rag_core/orchestrator/strategies/sql_strategy.py
import re
import logging
from typing import List, Dict, Any, Optional, Tuple, Iterator
from sqlalchemy.orm import Session
# pyrefly: ignore [missing-import]
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
from modules.rag_core.context.models import EntityScope

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

    # This is not a good desgin
    # Semantic database mapping for banking / enterprise domains with primary & secondary weights
    DOMAIN_TABLE_KEYWORDS = {
        "noros_customer_db": {
            "primary": [
                "bvn", "nin", "kyc", "identity", "customer_profile", "customer_master", "who is",
                "find customer", "look up customer", "address", "segment", "organization", "organizations",
                "company", "companies", "employer", "employers", "industry", "industries", "sector", "sectors",
                "corporate", "serving", "clientele", "employee who are our customers", "employees who are our customers",
                "their employee", "their employees", "employee of", "employees of", "employed by", "works at", "work at"
            ],
            "secondary": ["customer", "customers", "client", "person", "individual"]
        },
        "noros_core_banking_db": {
            "primary": ["account", "accounts", "deposit", "deposits", "balance", "balances", "inflow", "inflows", "outflow", "outflows", "transaction", "transactions", "standing_order", "beneficiary", "beneficiaries", "ledger"],
            "secondary": ["bank", "banking", "statement"]
        },
        "noros_lending_db": {
            "primary": ["loan", "loans", "lending", "facility", "facilities", "credit", "repayment", "repayments", "installment", "installments", "collateral", "amortization", "borrower"],
            "secondary": ["principal", "interest_rate"]
        },
        "noros_payments_db": {
            "primary": ["transfer", "transfers", "payment", "payments", "bill", "card", "pos", "merchant", "merchants"],
            "secondary": ["checkout", "terminal"]
        },
        "noros_compliance_db": {
            "primary": ["compliance", "aml", "sanction", "sanctions", "fraud", "suspicious", "alert", "alerts"],
            "secondary": ["screening", "flagged"]
        },
        "noros_operations_db": {
            "primary": ["branch", "branches", "atm", "atms", "relationship_manager", "relationship_managers", "bank employee", "bank employees", "internal employee", "internal employees", "bank staff"],
            "secondary": ["operation", "operations", "employee", "employees", "staff"]
        }
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
        Uses scored domain keyword matching and schema chunk verification.
        """
        if not db_jobs:
            return None
        if len(db_jobs) == 1:
            return db_jobs[0]

        job_map = {j.name.lower(): j for j in db_jobs}
        lower_query = query.lower()
        scores = {db_name: 0 for db_name in job_map}

        # 1. Direct database exact name in query (+40 pts)
        for db_name in job_map:
            if db_name in lower_query:
                scores[db_name] += 40

        # 2. Score based on domain keywords (primary = 25 pts, secondary = 2 pts)
        for db_name, kw_groups in self.DOMAIN_TABLE_KEYWORDS.items():
            if db_name not in scores:
                continue
            primaries = kw_groups.get("primary", [])
            secondaries = kw_groups.get("secondary", [])
            for p in primaries:
                if re.search(r"\b" + re.escape(p) + r"\b", lower_query):
                    scores[db_name] += 25
            for s in secondaries:
                if re.search(r"\b" + re.escape(s) + r"\b", lower_query):
                    scores[db_name] += 2

        # 3. Inspect schema chunks for database name header or table names (+5 pts)
        for chunk in schema_chunks[:3]:
            payload = getattr(chunk, "payload", {}) if hasattr(chunk, "payload") else (chunk.get("payload", {}) if isinstance(chunk, dict) else {})
            content = payload.get("content", "").lower()
            filename = payload.get("filename", "").lower()
            for db_name in job_map:
                if db_name in content or db_name in filename:
                    scores[db_name] += 5

        # 4. Disambiguation Boost: When inquiring about customers/clientele, boost noros_customer_db
        has_customer_kw = any(re.search(r"\b" + re.escape(w) + r"\b", lower_query) for w in [
            "customer", "customers", "client", "clients", "our customer", "our customers", "who are our customers"
        ])
        if has_customer_kw and "noros_customer_db" in scores:
            scores["noros_customer_db"] += 30
            # If asking about customers, penalize noros_operations_db unless branch/atm is explicitly asked
            if not any(re.search(r"\b" + re.escape(w) + r"\b", lower_query) for w in ["branch", "branches", "atm", "atms", "relationship_manager"]):
                scores["noros_operations_db"] = max(0, scores.get("noros_operations_db", 0) - 20)

        # 5. Core Banking Boost: When inquiring about accounts or balances, boost noros_core_banking_db
        has_balance_kw = any(re.search(r"\b" + re.escape(w) + r"\b", lower_query) for w in [
            "account balance", "account balances", "balances", "balance", "total deposit", "deposit balance", "deposit accounts", "bank accounts"
        ])
        if has_balance_kw and "noros_core_banking_db" in scores:
            scores["noros_core_banking_db"] += 50

        best_db, best_score = max(scores.items(), key=lambda x: x[1])
        if best_score > 0:
            logger.info(f"[DYNAMIC SQL] Matched target database '{best_db}' (score: {best_score}).")
            return job_map[best_db]

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

    def _resolve_customer_id_by_bvn(
        self,
        bvn: str,
        user_context: TokenData,
        db: Optional[Session]
    ) -> Optional[int]:
        """
        Fast cross-database identity bridge: Resolves a regulatory 11-digit BVN
        to customer_id from noros_customer_db so domain-isolated databases
        (noros_lending_db, noros_core_banking_db) can filter by primary key.
        """
        if not db or not bvn:
            return None
        try:
            from modules.governance.domain.models import IngestionJob
            from core.crypto import decrypt_connection_config
            from modules.connectors.security.sql_guard import SQLSecurityGuard
            from sqlalchemy import create_engine, text

            cust_job = db.query(IngestionJob).filter(
                IngestionJob.org_id == user_context.org_id,
                IngestionJob.name == "noros_customer_db"
            ).first()
            if not cust_job:
                return None

            decrypted = decrypt_connection_config(cust_job.connection_config)
            dialect = SQLSecurityGuard.canonical_dialect(cust_job.source_type)
            url = SQLSecurityGuard.safe_build_db_url(dialect, decrypted)
            engine = create_engine(url)
            with engine.connect() as conn:
                row = conn.execute(
                    text("SELECT customer_id FROM bvn_records WHERE bvn = :bvn LIMIT 1"),
                    {"bvn": bvn}
                ).fetchone()
                if row and row[0] is not None:
                    return int(row[0])
        except Exception as e:
            logger.warning(f"[DYNAMIC SQL] Failed cross-database BVN resolution: {e}")
        return None

    def execute_stream(
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
    ) -> Iterator[Tuple[str, Any]]:
        if not db:
            yield ("result", None)
            return

        org_name = kwargs.get("org_name", "Enterprise")

        try:
            db_jobs = db.query(IngestionJob).filter(
                IngestionJob.org_id == user_context.org_id,
                IngestionJob.source_type.in_(self.SUPPORTED_SOURCE_TYPES)
            ).all()

            if not db_jobs:
                logger.info("[DYNAMIC SQL] No database connector jobs configured for org.")
                yield ("result", None)
                return

            # 1. Resolve Target Database Connector
            yield ("status", {
                "step": "sql_target_resolution",
                "stage": "sql",
                "title": "Resolving Target Database",
                "details": "Matching query against registered enterprise database connectors...",
                "status": "in_progress"
            })

            query_vector = self.llm.get_embeddings(query)
            probe_hits = self.vector_store.search_vectors(
                query_vector=query_vector,
                search_filter=Filter(must=[FieldCondition(key="org_id", match=MatchValue(value=user_context.org_id))]),
                limit=5,
                score_threshold=0.20
            )

            job = self._resolve_target_job(query, db_jobs, probe_hits)
            if not job:
                yield ("status", {
                    "step": "sql_target_resolution",
                    "stage": "sql",
                    "title": "Target Database",
                    "details": "No matching database connector found for query.",
                    "status": "failed"
                })
                yield ("result", None)
                return

            decrypted_config = decrypt_connection_config(job.connection_config)
            dialect = SQLSecurityGuard.canonical_dialect(job.source_type)
            db_url = SQLSecurityGuard.safe_build_db_url(dialect, decrypted_config)

            # 2. Get Schema Context (DDLs)
            schema_context, _ = self._get_schema_context(query, user_context, job, dialect, db_url, db=db)
            if not schema_context.strip():
                logger.warning(f"[DYNAMIC SQL] No schema context available for target database '{job.name}'.")
                yield ("status", {
                    "step": "sql_target_resolution",
                    "stage": "sql",
                    "title": "Target Database",
                    "details": f"No schema DDLs available for {job.name}.",
                    "status": "failed"
                })
                yield ("result", None)
                return

            yield ("status", {
                "step": "sql_target_resolution",
                "stage": "sql",
                "title": "Target Database Identified",
                "details": f"Matched target database '{job.name}' ({dialect.upper()})",
                "status": "completed"
            })

            # Injected Context Bindings from SessionWorkingMemory & Cross-Engine Bridge
            working_memory = kwargs.get("working_memory")
            query_with_bindings = query
            bindings = []

            # Cross-database Identity Bridge:
            # If target database is domain-isolated (noros_lending_db or noros_core_banking_db)
            # and customer_id is not yet bound, resolve customer_id from BVN via noros_customer_db
            if job.name in ("noros_lending_db", "noros_core_banking_db"):
                has_cid = working_memory and "customer_id" in working_memory.active_entities
                if not has_cid:
                    bvn_cand = (working_memory.active_entities.get("bvn") if working_memory else None)
                    if not bvn_cand:
                        bvn_m = re.search(r"\b(\d{11})\b", query)
                        if bvn_m:
                            bvn_cand = bvn_m.group(1)
                    if bvn_cand:
                        resolved_cid = self._resolve_customer_id_by_bvn(bvn_cand, user_context, db)
                        if resolved_cid:
                            if working_memory:
                                working_memory.active_entities["customer_id"] = resolved_cid
                                working_memory.primary_anchor_id = resolved_cid
                            else:
                                bindings.append(f"customer_id = {resolved_cid}")
                            logger.info(f"[DYNAMIC SQL] Cross-engine identity bridge: resolved BVN {bvn_cand} to customer_id {resolved_cid}")

            if working_memory:
                if getattr(working_memory, "scope", None) not in (EntityScope.AGGREGATE.value, EntityScope.SYSTEM_META.value):
                    if getattr(working_memory, "active_entities", None):
                        bindings.extend(f"{k} = {repr(v)}" for k, v in working_memory.active_entities.items())
                    if getattr(working_memory, "collection_ids", None) and getattr(working_memory, "scope", None) == EntityScope.COLLECTION.value:
                        cids_str = ", ".join(str(c) for c in working_memory.collection_ids)
                        bindings.append(f"customer_id IN ({cids_str})")

            if bindings:
                bindings_str = "\n[CONTEXT BINDINGS: " + ", ".join(bindings) + "]"
                query_with_bindings = query + bindings_str
                logger.info(f"[DYNAMIC SQL] Injected context bindings: {bindings_str.strip()}")

            # 3. Dynamic SQL Agent Generation & Read-Only Execution
            yield ("status", {
                "step": "sql_synthesis",
                "stage": "sql",
                "title": "Synthesizing SQL Query",
                "details": f"Synthesizing dialect-tailored SQL query for {job.name} with AST safety guardrails...",
                "status": "in_progress"
            })

            sql_result = DynamicSQLAgent.generate_and_execute_sql(
                user_query=query_with_bindings,
                schema_context=schema_context,
                dialect=dialect,
                db_url=db_url,
                llm_service=self.llm
            )

            if sql_result.get("status") == "success":
                rows = sql_result.get("rows", [])
                cols = sql_result.get("columns", [])
                executed_sql = sql_result.get("sql", "")

                yield ("status", {
                    "step": "sql_synthesis",
                    "stage": "sql",
                    "title": "SQL Query Generated",
                    "details": f"Generated SQL: {executed_sql}",
                    "status": "completed"
                })

                yield ("status", {
                    "step": "sql_execution",
                    "stage": "sql",
                    "title": "Executing SQL Query",
                    "details": f"Executed sandboxed query against {job.name}. Retrieved {len(rows)} matching rows.",
                    "status": "completed"
                })

                if rows:
                    md_table_lines = ["| " + " | ".join(cols) + " |", "| " + " | ".join(["---"] * len(cols)) + " |"]
                    for r in rows[:15]:
                        row_vals = [str(r.get(c, "N/A")) for c in cols]
                        md_table_lines.append("| " + " | ".join(row_vals) + " |")
                    data_representation = "\n".join(md_table_lines)
                else:
                    data_representation = "The database query returned 0 matching records."

                table_requested = any(kw in query.lower() for kw in ["table", "tabular", "in a table", "as a table", "grid"])
                format_directive = (
                    "Format your response using a clean Markdown table since the user explicitly requested tabular format."
                    if table_requested else
                    "Deliver a direct, fluent, and well-structured answer in natural language with clear bullet points where appropriate. "
                    "Do NOT dump or append raw database tables or column matrices."
                )

                synthesis_prompt = (
                    f"User Inquiry: {query}\n"
                    f"Executed SQL: {executed_sql}\n"
                    f"Database Query Results ({len(rows)} matching records):\n{data_representation}\n\n"
                    f"Directives:\n"
                    f"- {format_directive}\n"
                    f"- Clearly state the exact customer names, identifiers, metrics, and pertinent attributes retrieved.\n"
                    f"- Be concise, direct, and professional without unnecessary boilerplate."
                )

                yield ("status", {
                    "step": "synthesis",
                    "stage": "synthesis",
                    "title": "Generating Response",
                    "details": "Generating final synthesized response from database records...",
                    "status": "in_progress"
                })

                answer = ""
                if hasattr(self.llm, "stream_text"):
                    for token in self.llm.stream_text(
                        synthesis_prompt,
                        system_instruction=f"You are an enterprise data analyst assistant for {org_name}. Provide clear, professional, and accurate answers directly answering the user inquiry based on the database records."
                    ):
                        answer += token
                        yield ("delta", {"content": token})
                else:
                    answer = self.llm.generate_text(
                        synthesis_prompt,
                        system_instruction=f"You are an enterprise data analyst assistant for {org_name}. Provide clear, professional, and accurate answers directly answering the user inquiry based on the database records."
                    )
                    yield ("delta", {"content": answer})

                full_answer = answer.strip()

                yield ("status", {
                    "step": "synthesis",
                    "stage": "synthesis",
                    "title": "Response Complete",
                    "details": "Synthesis completed successfully.",
                    "status": "completed"
                })


                sources = [{
                    "source_name": f"{job.name} ({dialect.upper()} Database)",
                    "filename": f"{job.name} ({dialect.upper()})",
                    "chunk_id": "sql_execution",
                    "sql_query": executed_sql,
                    "row_count": len(rows),
                    "relevance_score": 0.95,
                    "preview": f"SQL: {executed_sql}",
                    "rows": rows,
                    "database_name": job.name
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

                yield ("result", (full_answer, sources, True, 0.95))
            else:
                raw_err = sql_result.get('error', 'Execution error')
                logger.warning(f"[DYNAMIC SQL] Execution failed: {raw_err}.")
                
                # Check if this was a cross-database query (e.g. transaction volume in core banking + loans in lending)
                lower_q = query.lower()
                is_cross_db = (
                    any(k in lower_q for k in ["transaction", "transactions", "inflow", "deposit"]) and
                    any(k in lower_q for k in ["loan", "loans", "facility", "facilities", "credit"])
                )
                
                if is_cross_db:
                    error_details = (
                        "The requested inquiry spans two isolated database systems:\n"
                        "- **Transaction activity and account balances** reside in the Core Banking database (`noros_core_banking_db`).\n"
                        "- **Loan portfolios and credit facilities** reside in the Lending database (`noros_lending_db`).\n\n"
                        "Because these systems run on disparate database engines, they cannot be joined directly in a single SQL query. "
                        "Please query each system individually (for example, first request active loans from the lending system, then inspect 12-month transaction volumes for those specific accounts)."
                    )
                else:
                    error_details = (
                        f"I encountered a database execution issue while querying **{job.name}** ({dialect.upper()}): {raw_err}. "
                        "The requested data could not be retrieved from the relational schema."
                    )

                yield ("status", {
                    "step": "sql_execution",
                    "stage": "sql",
                    "title": "SQL Execution Error",
                    "details": f"Query error: {raw_err}",
                    "status": "failed"
                })

                yield ("delta", {"content": error_details})

                sources = [{
                    "source_name": f"{job.name} ({dialect.upper()} Database)",
                    "filename": f"{job.name} ({dialect.upper()})",
                    "chunk_id": "sql_error",
                    "sql_query": sql_result.get("sql", ""),
                    "row_count": 0,
                    "relevance_score": 0.0,
                    "preview": f"Database execution error: {raw_err}",
                    "error": True,
                    "database_name": job.name
                }]
                yield ("result", (error_details, sources, False, 0.0))
        except Exception as e:
            logger.warning(f"[DYNAMIC SQL] Exception in execute_stream: {e}.")
            err_text = f"An error occurred while executing the database query against the relational system: {e}"
            yield ("delta", {"content": err_text})
            sources = [{
                "source_name": "Relational Database Engine",
                "filename": "database_error",
                "chunk_id": "sql_exception",
                "sql_query": "",
                "row_count": 0,
                "relevance_score": 0.0,
                "preview": str(e),
                "error": True
            }]
            yield ("result", (err_text, sources, False, 0.0))

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
        """Synchronous wrapper consuming execute_stream for backward compatibility."""
        stream = self.execute_stream(
            query=query,
            user_context=user_context,
            db=db,
            session_id=session_id,
            persona_id=persona_id,
            template_id=template_id,
            top_k=top_k,
            score_threshold=score_threshold,
            mode=mode,
            **kwargs
        )
        for item_type, data in stream:
            if item_type == "result":
                return data
        return None
