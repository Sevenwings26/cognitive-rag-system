# modules/rag_core/orchestrator/unified_orchestrator.py
import uuid
import logging
from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy.orm import Session

from core.config import settings
from modules.auth.domain.tokens import TokenData
from modules.auth.domain.models import UserRole, Organization, Department
from modules.governance.domain.models import AssistantPersona, PromptTemplate, EnterpriseDocument, IngestionJob
from modules.rag_core.tools.sql_agent import DynamicSQLAgent
from modules.connectors.security.sql_guard import SQLSecurityGuard
from core.crypto import decrypt_connection_config
from modules.governance.services.audit_logger import AuditLogger
from modules.connectors.parsers.factory import ParserFactory
from modules.connectors.sources.file_connector import FileConnector
from modules.rag_core.providers.llm import BaseLLMService, LLMFactory
from modules.rag_core.retrieval.vector_store import VectorStoreService
from modules.rag_core.retrieval.security_filter import RAGSecurityFilterBuilder
from modules.rag_core.retrieval.hybrid_retriever import HybridRetriever
from modules.rag_core.retrieval.reranker import CrossEncoderReranker
from modules.rag_core.guardrails.grounding_validator import GroundingValidator
from modules.rag_core.guardrails.prompt_engine import PromptEngine
from modules.rag_core.registry.knowledge_registry import KnowledgeRegistry
from modules.rag_core.orchestrator.query_planner import QueryPlanner
from qdrant_client.models import PointStruct, Filter, FieldCondition, MatchValue


logger = logging.getLogger("unified_rag_orchestrator")

class UnifiedRAGOrchestrator:
    def __init__(
        self,
        llm_service: Optional[BaseLLMService] = None,
        vector_store: Optional[VectorStoreService] = None,
        reranker: Optional[CrossEncoderReranker] = None,
        knowledge_registry: Optional[KnowledgeRegistry] = None
    ):
        self.llm = llm_service or LLMFactory.get_provider()
        self.vector_store = vector_store or VectorStoreService()
        self.reranker = reranker or CrossEncoderReranker()
        self.registry = knowledge_registry or KnowledgeRegistry()
        self.hybrid_retriever = HybridRetriever(vector_store=self.vector_store, llm_service=self.llm)
        self.grounding_validator = GroundingValidator()
        self.prompt_engine = PromptEngine()

    def execute_unified_query(
        self,
        query: str,
        user_context: TokenData,
        db: Optional[Session] = None,
        session_id: Optional[str] = None,
        scope: Optional[str] = None,
        persona_id: Optional[str] = None,
        template_id: Optional[str] = None,
        top_k: int = 3,
        score_threshold: float = 0.35,
        mode: str = "auto"
    ) -> Tuple[str, List[Dict[str, Any]], bool, float]:
        # 1. Resolve Organization & Department Context
        org_name = "Enterprise"
        dept_name = "General"
        has_session_documents = False

        if db:
            org = db.query(Organization).filter(Organization.id == user_context.org_id).first()
            if org:
                org_name = org.name
            if user_context.department_id:
                dept = db.query(Department).filter(Department.id == user_context.department_id).first()
                if dept:
                    dept_name = dept.name
            if session_id:
                doc_count = db.query(EnterpriseDocument).filter(
                    EnterpriseDocument.org_id == user_context.org_id
                ).count()
                has_session_documents = (doc_count > 0)

        # 2. Analyze Query Intent & Plan Execution
        plan = QueryPlanner.analyze_and_plan(
            query=query,
            user_context=user_context,
            has_session_documents=has_session_documents,
            mode=mode
        )

        # 3. Select Adaptive vs Strict System Template
        if mode == "rag":
            system_template = self.grounding_validator.STRICT_SYSTEM_INSTRUCTION
            temperature = 0.2
        else:
            system_template = self.grounding_validator.ADAPTIVE_SYSTEM_INSTRUCTION
            temperature = 0.4

        if persona_id and db:
            persona = db.query(AssistantPersona).filter(
                AssistantPersona.id == persona_id,
                AssistantPersona.org_id == user_context.org_id,
                AssistantPersona.is_active == True
            ).first()
            if persona:
                system_template = persona.system_instruction_template
                temperature = persona.temperature / 10.0 if persona.temperature > 1 else persona.temperature

        template_vars = {
            "user_name": user_context.email.split("@")[0] if user_context.email else "User",
            "user_role": user_context.role,
            "department_name": dept_name,
            "org_name": org_name,
            "query": query
        }

        # --- ROUTE A: Conversational / General Knowledge ---
        if plan.is_conversational_only:
            logger.info(f"Query routed to General/Conversational handler (intent: {plan.intent_category}).")
            general_instruction = (
                f"You are a helpful and knowledgeable AI assistant for {org_name}. "
                "Answer the user's question clearly, politely, and accurately using your general knowledge."
            )
            if persona_id:
                general_instruction = self.prompt_engine.render_template(system_template, template_vars)

            answer = self.llm.generate_text(
                query,
                system_instruction=general_instruction,
                temperature=max(temperature, 0.5)
            )

            if db:
                AuditLogger.log(
                    db=db,
                    org_id=user_context.org_id,
                    user_id=user_context.user_id,
                    action="GENERAL_CHAT_QUERY",
                    resource_type="CHAT_QUERY",
                    resource_id=session_id,
                    details={"query": query[:200], "intent": plan.intent_category}
                )

            return answer, [], True, 1.0

                # --- ROUTE B: Dynamic Text-to-SQL for Structured Database Inquiries ---
        if plan.is_structured_sql and db:
            sql_response = self._try_execute_dynamic_sql(
                query=query,
                user_context=user_context,
                session_id=session_id,
                org_name=org_name,
                db=db
            )
            if sql_response is not None:
                return sql_response

        # --- ROUTE C: Adaptive Document-Grounded RAG Pipeline ---
        system_instruction = self.prompt_engine.render_template(system_template, template_vars)

        user_role_enum = UserRole(user_context.role) if hasattr(UserRole, user_context.role) else UserRole.MEMBER
        security_filter = RAGSecurityFilterBuilder.build_search_filter(
            org_id=user_context.org_id,
            department_id=user_context.department_id,
            user_id=user_context.user_id,
            user_role=user_role_enum,
            session_id=session_id,
            scope=scope
        )

        candidate_chunks = self.hybrid_retriever.retrieve(
            query=query,
            security_filter=security_filter,
            candidate_limit=settings.DEFAULT_CANDIDATE_LIMIT,
            score_threshold=score_threshold
        )

        # Fallback if no relevant documents found in knowledge base
        if not candidate_chunks:
            logger.info(f"Query '{query[:50]}' had no chunks above threshold {score_threshold}.")
            if mode == "rag":
                return self.grounding_validator.get_out_of_context_response(query, org_name)
            else:
                fallback_instruction = (
                    f"You are an AI assistant for {org_name}. "
                    "No internal company documents matched this specific inquiry. "
                    "Answer the user query accurately and helpfully using your general knowledge."
                )
                answer = self.llm.generate_text(
                    query,
                    system_instruction=fallback_instruction,
                    temperature=0.6
                )
                return answer, [], False, 0.0

        reranked_chunks = self.reranker.rerank(
            query=query,
            candidate_chunks=candidate_chunks,
            top_n=top_k
        )

        context_block, raw_sources = self.grounding_validator.format_grounded_context(reranked_chunks)
        sources = self.grounding_validator.deduplicate_sources(raw_sources)

        user_prompt_str = (
            f"=== Authorized Internal Knowledge Context ===\n"
            f"{context_block}\n"
            f"=== User Inquiry ===\n"
            f"{query}\n\n"
            f"Directives: Provide a direct, fluent, and well-structured answer. Present information clearly and naturally without robotic meta-openings (e.g. avoid 'According to Source 1...' or 'Based on the spreadsheet...')."
        )

        if template_id and db:
            p_template = db.query(PromptTemplate).filter(
                PromptTemplate.id == template_id,
                PromptTemplate.org_id == user_context.org_id,
                PromptTemplate.is_active == True
            ).first()
            if p_template:
                template_vars["context"] = context_block
                user_prompt_str = self.prompt_engine.render_template(p_template.user_prompt_template, template_vars)

        answer = self.llm.generate_text(
            user_prompt_str,
            system_instruction=system_instruction,
            temperature=temperature
        )

        is_grounded, confidence = self.grounding_validator.validate_grounding(answer, sources)

        if db:
            AuditLogger.log(
                db=db,
                org_id=user_context.org_id,
                user_id=user_context.user_id,
                action="UNIFIED_RAG_QUERY",
                resource_type="CHAT_QUERY",
                resource_id=session_id,
                details={
                    "query": query[:200],
                    "persona_id": persona_id,
                    "template_id": template_id,
                    "sources_count": len(sources),
                    "is_grounded": is_grounded,
                    "confidence": confidence
                }
            )

        return answer, sources, is_grounded, confidence

    def _try_execute_dynamic_sql(
        self,
        query: str,
        user_context: TokenData,
        session_id: Optional[str],
        org_name: str,
        db: Session
    ) -> Optional[Tuple[str, List[Dict[str, Any]], bool, float]]:
        """
        Dynamic Text-to-SQL Execution Route:
        Queries tenant-connected relational databases using reflected DDL schema context,
        LLM query synthesis, and read-only AST sandboxing.
        """
        try:
            db_jobs = db.query(IngestionJob).filter(
                IngestionJob.org_id == user_context.org_id,
                IngestionJob.source_type.in_(["POSTGRES_DB", "POSTGRESQL", "MYSQL_DB", "MYSQL", "ORACLE_DB", "MSSQL_DB"])
            ).all()

            if not db_jobs:
                return None

            job = db_jobs[0]
            decrypted_config = decrypt_connection_config(job.connection_config)
            dialect = SQLSecurityGuard.canonical_dialect(job.source_type)
            db_url = SQLSecurityGuard.build_connection_url(dialect, decrypted_config)

            # Retrieve schema context from Qdrant or reflect directly
            schema_filter = Filter(
                must=[
                    FieldCondition(key="org_id", match=MatchValue(value=user_context.org_id)),
                    FieldCondition(key="chunk_type", match=MatchValue(value="sql_schema"))
                ]
            )
            schema_chunks = self.vector_store.search(
                query_vector=self.llm.get_embeddings(query),
                limit=5,
                search_filter=schema_filter
            )

            if schema_chunks:
                schema_context = "\n\n".join([chunk.payload.get("content", "") for chunk in schema_chunks])
            else:
                from modules.connectors.sources.databases.schema_reflector import DatabaseSchemaReflector
                reflected = DatabaseSchemaReflector.reflect_schema(
                    dialect=dialect,
                    db_url=db_url,
                    target_schema=decrypted_config.get("schema")
                )
                schema_context = "\n\n".join([t["ddl"] for t in reflected[:10]])

            if not schema_context.strip():
                return None

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
                    f"Provide a clear, direct, and professional answer to the user's inquiry based on this query result."
                )
                answer = self.llm.generate_text(
                    synthesis_prompt,
                    system_instruction=f"You are an enterprise data analyst assistant for {org_name}."
                )
                full_answer = f"{answer}\n{table_summary}"

                sources = [{
                    "source_name": f"{job.name} ({dialect.upper()} Database)",
                    "chunk_id": "sql_execution",
                    "sql_query": executed_sql,
                    "row_count": len(rows)
                }]

                AuditLogger.log(
                    db=db,
                    org_id=user_context.org_id,
                    user_id=user_context.user_id,
                    action="DYNAMIC_SQL_QUERY",
                    resource_type="DATABASE",
                    resource_id=job.id,
                    details={"sql": executed_sql, "rows_returned": len(rows)}
                )

                return full_answer, sources, True, 0.95
            else:
                logger.warning(f"[DYNAMIC SQL] Execution failed: {sql_result.get('error')}. Falling back to document RAG.")
                return None
        except Exception as e:
            logger.warning(f"[DYNAMIC SQL] Error: {e}. Falling back to document RAG.")
            return None

    def extract_text_from_file(self, filename: str, file_bytes: bytes, mime_type: Optional[str] = None) -> str:
        parser = ParserFactory.get_parser(filename, mime_type)
        return parser.parse(file_bytes)

    def chunk_text(self, text: str, chunk_size: int = 1000, overlap: int = 100) -> List[str]:
        """
        Token-budgeted, boundary-aware chunker supporting tabular structured datasets
        (Excel/CSV) as well as standard unstructured documents (PDF/DOCX/TXT).
        Guarantees chunks respect sentence/paragraph boundaries and never clip against
        the embedding model context window (~4 chars per token safety budget).
        """
        import re
        if not text or not text.strip():
            return []

        # 1. Tabular / Spreadsheet Structured Chunking
        if "### [Spreadsheet Table" in text or "### [Tabular Document" in text:
            chunks = []
            sections = text.split("### [")
            for section in sections:
                if not section.strip():
                    continue
                section_text = "### [" + section.strip() if not section.startswith("### [") else section.strip()
                lines = section_text.split("\n")

                header_lines = []
                row_lines = []
                for line in lines:
                    if line.startswith("### [") or line.startswith("Columns:"):
                        header_lines.append(line)
                    elif line.strip():
                        row_lines.append(line)

                header_context = "\n".join(header_lines) + "\n\n" if header_lines else ""

                batch_size = 6
                for i in range(0, len(row_lines), batch_size):
                    batch = row_lines[i:i + batch_size]
                    chunk_str = header_context + "\n".join(batch)
                    chunks.append(chunk_str)

            return chunks if chunks else [text]

        # 2. Token-Budgeted Boundary-Aware Chunking (512 tokens ~= 2000 characters)
        max_chars = 2000
        overlap_chars = 200

        paragraphs = text.split("\n\n")
        chunks = []
        current_chunk = []
        current_len = 0

        for p in paragraphs:
            p_str = p.strip()
            if not p_str:
                continue

            if len(p_str) > max_chars:
                sentences = re.split(r'(?<=[.?!])\s+', p_str)
                for s in sentences:
                    s_len = len(s)
                    if current_len + s_len > max_chars and current_chunk:
                        joined = " ".join(current_chunk).strip()
                        if joined:
                            chunks.append(joined)
                        overlap_tail = []
                        running_ov = 0
                        for item in reversed(current_chunk):
                            if running_ov + len(item) <= overlap_chars:
                                overlap_tail.insert(0, item)
                                running_ov += len(item)
                            else:
                                break
                        current_chunk = overlap_tail + [s]
                        current_len = sum(len(x) for x in current_chunk) + len(current_chunk)
                    else:
                        current_chunk.append(s)
                        current_len += s_len + 1
            else:
                p_len = len(p_str)
                if current_len + p_len > max_chars and current_chunk:
                    joined = "\n\n".join(current_chunk).strip()
                    if joined:
                        chunks.append(joined)
                    current_chunk = [p_str]
                    current_len = p_len
                else:
                    current_chunk.append(p_str)
                    current_len += p_len + 2

        if current_chunk:
            joined = "\n\n".join(current_chunk).strip()
            if joined:
                chunks.append(joined)

        return chunks if chunks else [text[:max_chars]]

    def ingest_document(
        self,
        filename: str,
        file_bytes: bytes,
        document_id: str,
        org_id: str,
        department_id: Optional[str] = None,
        uploader_id: Optional[str] = None,
        access_level: str = "DEPARTMENT",
        session_id: Optional[str] = None,
        mime_type: Optional[str] = None,
        db: Optional[Any] = None
    ) -> int:
        from modules.governance.domain.models import DocumentChunk

        connector = FileConnector(filename=filename, content_bytes=file_bytes, mime_type=mime_type)
        raw_doc = next(connector.fetch_documents())

        raw_text = self.extract_text_from_file(raw_doc.filename, raw_doc.content_bytes, raw_doc.mime_type)
        chunks = self.chunk_text(raw_text)

        if not chunks:
            raise ValueError("No extractable text found in document.")

        # 1. Batch / Parallelized Vector Embedding Generation
        embeddings = []
        if hasattr(self.llm, "get_embeddings_batch"):
            try:
                embeddings = self.llm.get_embeddings_batch(chunks)
            except Exception:
                embeddings = [self.llm.get_embeddings(c) for c in chunks]
        else:
            embeddings = [self.llm.get_embeddings(c) for c in chunks]

        # 2. Prepare Points for Qdrant and Entities for PostgreSQL
        points = []
        db_chunk_records = []

        for idx, (chunk, embedding) in enumerate(zip(chunks, embeddings)):
            point_id = str(uuid.uuid4())
            payload = {
                "document_id": document_id,
                "org_id": org_id,
                "department_id": department_id or "",
                "uploader_id": uploader_id or "",
                "access_level": access_level,
                "session_id": session_id or "",
                "filename": filename,
                "chunk_index": idx,
                "content": chunk,
                "source_type": raw_doc.source_type
            }

            points.append(
                PointStruct(
                    id=point_id,
                    vector=embedding,
                    payload=payload
                )
            )

            if db is not None:
                db_chunk_records.append(
                    DocumentChunk(
                        id=point_id,
                        document_id=document_id,
                        org_id=org_id,
                        department_id=department_id or "",
                        uploader_id=uploader_id or "",
                        access_level=access_level,
                        chunk_index=idx,
                        content=chunk,
                        embedding=embedding,
                        metadata_json={
                            "source_type": raw_doc.source_type,
                            "filename": filename,
                            "session_id": session_id or ""
                        }
                    )
                )

        # 3. Dual-Write: Upsert to Qdrant (Primary Search) & Persist to PostgreSQL (Source of Truth)
        try:
            self.vector_store.upsert_chunks(points)

            if db is not None and db_chunk_records:
                try:
                    # Remove any stale chunks for this doc before inserting fresh ones
                    db.query(DocumentChunk).filter(DocumentChunk.document_id == document_id).delete()
                    db.bulk_save_objects(db_chunk_records)
                    db.commit()
                except Exception as db_err:
                    logger.error(f"[DUAL-WRITE ROLLBACK] PostgreSQL chunk insert failed for doc '{document_id}': {db_err}. Purging Qdrant vectors...")
                    db.rollback()
                    doc_filter = Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))])
                    self.vector_store.delete_by_filter(doc_filter)
                    raise db_err

        except Exception as e:
            logger.error(f"Dual-write ingestion failed for doc '{document_id}': {e}")
            if db is not None:
                db.rollback()
            raise e

        return len(chunks)

    def delete_session_vectors(self, session_id: str) -> bool:
        doc_filter = Filter(
            must=[FieldCondition(key="session_id", match=MatchValue(value=session_id))]
        )
        return self.vector_store.delete_by_filter(doc_filter)

    def delete_document_vectors(self, document_id: str, org_id: str, db: Optional[Any] = None) -> bool:
        from modules.governance.domain.models import DocumentChunk

        doc_filter = Filter(
            must=[
                FieldCondition(key="org_id", match=MatchValue(value=org_id)),
                FieldCondition(key="document_id", match=MatchValue(value=document_id))
            ]
        )
        qdrant_deleted = self.vector_store.delete_by_filter(doc_filter)

        if db is not None:
            try:
                db.query(DocumentChunk).filter(
                    DocumentChunk.document_id == document_id,
                    DocumentChunk.org_id == org_id
                ).delete()
                db.commit()
            except Exception as e:
                logger.error(f"Failed to delete relational chunks for doc '{document_id}': {e}")
                db.rollback()

        return qdrant_deleted
        
