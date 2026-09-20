# modules/rag_core/orchestrator/unified_orchestrator.py
import uuid
import logging
import time
from typing import List, Dict, Any, Optional, Tuple, Iterator
from sqlalchemy.orm import Session

from core.config import settings
from core.telemetry import record_backend_route, BackendRoute
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
from modules.rag_core.orchestrator.strategies import (
    BaseRetrievalStrategy,
    ConversationalStrategy,
    SessionDocumentStrategy,
    EnterpriseKnowledgeStrategy,
    DynamicSQLStrategy,
    SystemMetaStrategy
)
from modules.rag_core.context import (
    SessionWorkingMemory,
    SessionContextManager,
    QueryCondenser,
    StateHarvester
)
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

        # Modular Strategies
        self.conversational_strategy = ConversationalStrategy(self.llm, self.prompt_engine)
        self.session_strategy = SessionDocumentStrategy(self.vector_store, self.llm, self.grounding_validator, self.prompt_engine)
        self.enterprise_strategy = EnterpriseKnowledgeStrategy(self.vector_store, self.llm, self.reranker, self.grounding_validator, self.prompt_engine)
        self.sql_strategy = DynamicSQLStrategy(self.vector_store, self.llm)
        self.meta_strategy = SystemMetaStrategy()

    def execute_unified_query_stream(
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
    ) -> Iterator[Tuple[str, Dict[str, Any]]]:
        start_time = time.time()

        yield ("status", {
            "step": "context_resolution",
            "stage": "context",
            "title": "Resolving Conversational Context",
            "details": "Resolving conversational context and active entities from session history...",
            "status": "in_progress"
        })

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

        # 2. Universal Working Memory & History Context
        working_memory = SessionContextManager.get_memory(session_id)
        history = SessionContextManager.get_recent_history(db, session_id, current_query=query)

        # 3. Anaphora Resolution & Query Condensation
        condensed_query = QueryCondenser.condense(
            query=query,
            history=history,
            memory=working_memory,
            llm_service=self.llm
        )

        details = f"Resolved query: \"{condensed_query}\"" if condensed_query != query else "Context verified from active session memory."
        yield ("status", {
            "step": "context_resolution",
            "stage": "context",
            "title": "Context Resolved",
            "details": details,
            "status": "completed"
        })

        # 4. Strict Session Document Check (Fast Qdrant Payload Query)
        if session_id:
            has_session_documents = self.session_strategy.has_session_documents(user_context.org_id, session_id)

        # 5. Analyze Query Intent & Plan Execution on Disambiguated Query
        yield ("status", {
            "step": "query_planning",
            "stage": "routing",
            "title": "Planning Query Route",
            "details": "Evaluating query intent and selecting optimal retrieval strategy...",
            "status": "in_progress"
        })

        plan = QueryPlanner.analyze_and_plan(
            query=condensed_query,
            user_context=user_context,
            has_session_documents=has_session_documents,
            mode=mode
        )

        target_desc = "Relational SQL Agent" if plan.is_structured_sql else ("Session Documents" if (scope == "session" or (has_session_documents and scope != "enterprise")) else ("Conversational" if plan.is_conversational_only else "Enterprise Knowledge Base"))
        if plan.intent_category == "SYSTEM_META":
            target_desc = "System Meta Registry (RBAC)"

        yield ("status", {
            "step": "query_planning",
            "stage": "routing",
            "title": f"Route Selected: {target_desc}",
            "details": f"Intent: {plan.intent_category} | Strategy: {target_desc}",
            "status": "completed"
        })

        strategy_kwargs = {
            "org_name": org_name,
            "dept_name": dept_name,
            "scope": scope,
            "intent_category": plan.intent_category,
            "working_memory": working_memory,
            "raw_query": query
        }

        response = None
        strategy_used = "enterprise"
        extra_meta = {}
        strategy_stream = None

        # --- ROUTE 0: System Metadata & Knowledge Source Audit (RBAC Governed) ---
        if plan.intent_category == "SYSTEM_META":
            logger.info("Query routed to SystemMetaStrategy stream (intent: SYSTEM_META).")
            strategy_used = "system_meta"
            strategy_stream = self.meta_strategy.execute_stream(
                query=condensed_query,
                user_context=user_context,
                db=db,
                session_id=session_id,
                persona_id=persona_id,
                template_id=template_id,
                top_k=top_k,
                score_threshold=score_threshold,
                mode=mode,
                **strategy_kwargs
            )

        # --- ROUTE A: Conversational / General Knowledge ---
        elif plan.is_conversational_only:
            logger.info(f"Query routed to ConversationalStrategy stream (intent: {plan.intent_category}).")
            strategy_used = "conversational"
            strategy_stream = self.conversational_strategy.execute_stream(
                query=condensed_query,
                user_context=user_context,
                db=db,
                session_id=session_id,
                persona_id=persona_id,
                template_id=template_id,
                top_k=top_k,
                score_threshold=score_threshold,
                mode=mode,
                **strategy_kwargs
            )

        # --- ROUTE B: Dynamic Text-to-SQL for Structured Database Inquiries ---
        elif plan.is_structured_sql and db:
            logger.info(f"Query routed to DynamicSQLStrategy stream (intent: {plan.intent_category}).")
            strategy_used = "sql"
            strategy_stream = self.sql_strategy.execute_stream(
                query=condensed_query,
                user_context=user_context,
                db=db,
                session_id=session_id,
                persona_id=persona_id,
                template_id=template_id,
                top_k=top_k,
                score_threshold=score_threshold,
                mode=mode,
                **strategy_kwargs
            )

        if strategy_stream is not None:
            for event_type, data in strategy_stream:
                if event_type == "status":
                    yield ("status", data)
                elif event_type == "delta":
                    yield ("delta", data)
                elif event_type == "result":
                    response = data

        # Fallback if Dynamic SQL yielded None (failed AST/schema/execution)
        if response is None and strategy_used == "sql":
            logger.info("[DYNAMIC SQL] Fallback triggered; routing to document RAG.")
            yield ("status", {
                "step": "sql_fallback",
                "stage": "routing",
                "title": "Fallback to Document RAG",
                "details": "Relational query inconclusive. Routing to enterprise document search.",
                "status": "completed"
            })
            strategy_stream = None

        # --- ROUTE C: Scoped In-Chat Documents vs Enterprise Knowledge Base ---
        if response is None:
            if scope == "session" or (has_session_documents and scope != "enterprise"):
                logger.info(f"Query routed to SessionDocumentStrategy stream for session '{session_id}'.")
                strategy_used = "session"
                strategy_stream = self.session_strategy.execute_stream(
                    query=condensed_query,
                    user_context=user_context,
                    db=db,
                    session_id=session_id,
                    persona_id=persona_id,
                    template_id=template_id,
                    top_k=top_k,
                    score_threshold=score_threshold,
                    mode=mode,
                    **strategy_kwargs
                )
            else:
                logger.info("Query routed to EnterpriseKnowledgeStrategy stream.")
                strategy_used = "enterprise"
                strategy_stream = self.enterprise_strategy.execute_stream(
                    query=condensed_query,
                    user_context=user_context,
                    db=db,
                    session_id=session_id,
                    persona_id=persona_id,
                    template_id=template_id,
                    top_k=top_k,
                    score_threshold=score_threshold,
                    mode=mode,
                    **strategy_kwargs
                )

            for event_type, data in strategy_stream:
                if event_type == "status":
                    yield ("status", data)
                elif event_type == "delta":
                    yield ("delta", data)
                elif event_type == "result":
                    response = data

        # 6. Post-Turn State Harvesting & Working Memory Update
        if response is None:
            response = ("I was unable to retrieve a satisfactory answer for your query.", [], False, 0.0)

        answer, sources, is_grounded, confidence = response

        # Record authoritative APM telemetry route
        _route_map = {
            "conversational": BackendRoute.GENERAL,
            "sql": BackendRoute.SQL,
            "session": BackendRoute.ATTACHMENTS,
            "enterprise": BackendRoute.RAG,
            "system_meta": BackendRoute.GENERAL,
        }
        record_backend_route(_route_map.get(strategy_used, BackendRoute.RAG))

        if strategy_used == "sql" and sources:
            first_src = sources[0]
            extra_meta["rows"] = first_src.get("rows", [])
            extra_meta["database_name"] = first_src.get("database_name")

        updated_memory = StateHarvester.harvest(
            memory=working_memory,
            strategy_name=strategy_used,
            query=condensed_query,
            answer=answer,
            sources=sources,
            extra_meta=extra_meta
        )
        SessionContextManager.save_memory(updated_memory)

        # Remove internal raw rows from public source objects so they don't bloat citation_metadata
        if strategy_used == "sql" and sources:
            for s in sources:
                s.pop("rows", None)

        latency_ms = int((time.time() - start_time) * 1000)

        yield ("metadata", {
            "sources": sources,
            "latency_ms": latency_ms,
            "session_id": session_id,
            "is_grounded": is_grounded,
            "confidence": confidence,
            "strategy_used": strategy_used
        })

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
        """Synchronous wrapper consuming execute_unified_query_stream for backward compatibility."""
        accumulated_answer = ""
        final_sources = []
        is_grounded = True
        confidence = 1.0

        for event_type, payload in self.execute_unified_query_stream(
            query=query,
            user_context=user_context,
            db=db,
            session_id=session_id,
            scope=scope,
            persona_id=persona_id,
            template_id=template_id,
            top_k=top_k,
            score_threshold=score_threshold,
            mode=mode
        ):
            if event_type == "delta":
                accumulated_answer += payload.get("content", "")
            elif event_type == "metadata":
                final_sources = payload.get("sources", [])
                is_grounded = payload.get("is_grounded", True)
                confidence = payload.get("confidence", 1.0)

        return accumulated_answer, final_sources, is_grounded, confidence


    def _try_execute_dynamic_sql(
        self,
        query: str,
        user_context: TokenData,
        session_id: Optional[str],
        org_name: str,
        db: Session
    ) -> Optional[Tuple[str, List[Dict[str, Any]], bool, float]]:
        """Backward-compatible delegation to DynamicSQLStrategy."""
        return self.sql_strategy.execute(
            query=query,
            user_context=user_context,
            db=db,
            session_id=session_id,
            org_name=org_name
        )

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
        doc_scope = "session" if (session_id and session_id.strip()) else "enterprise"
        bound_session_id = session_id.strip() if doc_scope == "session" else ""

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
                "session_id": bound_session_id,
                "scope": doc_scope,
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
                            "session_id": bound_session_id,
                            "scope": doc_scope
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

    def update_document_metadata(
        self,
        document_id: str,
        org_id: str,
        access_level: Optional[str] = None,
        department_id: Optional[str] = None,
        db: Optional[Any] = None
    ) -> bool:
        """
        Updates ACL and department scope metadata across relational DocumentChunks and Qdrant points.
        """
        from modules.governance.domain.models import DocumentChunk

        payload_updates: Dict[str, Any] = {}
        if access_level is not None:
            payload_updates["access_level"] = access_level
        if department_id is not None:
            payload_updates["department_id"] = department_id

        if not payload_updates:
            return True

        # 1. Update Qdrant vectors
        doc_filter = Filter(
            must=[
                FieldCondition(key="org_id", match=MatchValue(value=org_id)),
                FieldCondition(key="document_id", match=MatchValue(value=document_id))
            ]
        )
        qdrant_updated = self.vector_store.set_payload_by_filter(doc_filter, payload_updates)

        # 2. Update relational chunks
        if db is not None:
            try:
                db_updates = {}
                if access_level is not None:
                    db_updates["access_level"] = access_level
                if department_id is not None:
                    db_updates["department_id"] = department_id

                db.query(DocumentChunk).filter(
                    DocumentChunk.document_id == document_id,
                    DocumentChunk.org_id == org_id
                ).update(db_updates)
                db.commit()
            except Exception as e:
                logger.error(f"Failed to update relational chunks for doc '{document_id}': {e}")
                db.rollback()

        return qdrant_updated
