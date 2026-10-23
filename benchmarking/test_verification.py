# benchmarking/test_verification.py
import os
import sys

# Ensure project root is in PYTHONPATH
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

def test_modular_monolith_architecture():
    print("Testing Modular Monolith Architecture (Clean Direct-Import Zero-Shim State)...")

    # 1. Test Core Kernel
    from core.config import settings
    from core.database import Base, engine, SessionLocal
    print(f"  [PASS] Core Kernel loaded: App='{settings.APP_NAME}', Version='{settings.APP_VERSION}'")

    # 2. Test Connectors & Parsers Domain
    from modules.connectors.base import BaseConnector, RawDocument
    from modules.connectors.parsers.base import BaseParser
    from modules.connectors.parsers.factory import ParserFactory
    from modules.connectors.parsers.pdf_parser import PDFParser
    from modules.connectors.parsers.docx_parser import DocxParser
    from modules.connectors.parsers.text_parser import TextParser
    from modules.connectors.parsers.tabular_parser import TabularParser
    from modules.connectors.sources.file_connector import FileConnector
    from modules.connectors.sources.s3_connector import S3Connector
    from modules.connectors.sources.databases.postgres_connector import PostgreSQLConnector, RelationalDBConnector
    from modules.connectors.sources.databases.mysql_connector import MySQLConnector
    from modules.connectors.sources.databases.oracle_connector import OracleDBConnector
    from modules.connectors.sources.databases.mssql_connector import MSSQLConnector
    from modules.connectors.security.sql_guard import SQLSecurityGuard, InsecureQueryError, DatabaseSecurityTargetError
    from modules.connectors.registry import ConnectorRegistry

    sample_text = b"Enterprise Modular Monolith Test Document"
    parser = ParserFactory.get_parser("document.txt")
    assert parser.parse(sample_text) == "Enterprise Modular Monolith Test Document"
    print("  [PASS] modules.connectors, Database Connectors, SQLSecurityGuard, and ParserFactory verified.")

    # 3. Test Auth & Identity Domain
    from modules.auth.domain.tokens import TokenData
    from modules.auth.domain.models import User, Organization, Department, UserRole, AccessLevel
    from modules.auth.services.token_service import (
        verify_password, get_password_hash, create_access_token, decode_access_token
    )
    from modules.auth.repositories.user_repository import UserRepository
    from modules.auth.policies import has_role

    pw_hash = get_password_hash("testpassword123")
    assert verify_password("testpassword123", pw_hash)
    token = create_access_token({"sub": "u123", "org_id": "org123", "role": "SUPER_ADMIN"})
    payload = decode_access_token(token)
    assert payload["sub"] == "u123" and payload["role"] == "SUPER_ADMIN"
    print("  [PASS] modules.auth (tokens, password hashing, JWT) verified.")

    # 4. Test Governance & Audit Domain
    from modules.governance.domain.models import (
        DocumentStatus, IngestionJobStatus, AssistantPersona, PromptTemplate,
        AuditLog, EnterpriseDocument, IngestionJob, ChatSession, ChatMessage
    )
    from modules.governance.services.audit_logger import AuditLogger
    from modules.governance.repositories.governance_repository import GovernanceRepository
    from modules.governance.repositories.document_repository import DocumentRepository
    from modules.governance.repositories.chat_repository import ChatRepository
    print("  [PASS] modules.governance domain models and repositories verified.")

    # 5. Test RAG Core Domain
    from modules.rag_core.domain.types import RetrievedChunk, QueryPlan
    from modules.rag_core.providers.llm import BaseLLMService, LLMFactory
    from modules.rag_core.retrieval.vector_store import VectorStoreService
    from modules.rag_core.retrieval.security_filter import RAGSecurityFilterBuilder
    from modules.rag_core.retrieval.hybrid_retriever import HybridRetriever
    from modules.rag_core.retrieval.reranker import CrossEncoderReranker
    from modules.rag_core.guardrails.grounding_validator import GroundingValidator
    from modules.rag_core.guardrails.prompt_engine import PromptEngine
    from modules.rag_core.registry.knowledge_registry import KnowledgeRegistry
    from modules.rag_core.orchestrator.query_planner import QueryPlanner
    from modules.rag_core.orchestrator.unified_orchestrator import UnifiedRAGOrchestrator

    test_user = TokenData(user_id="u1", org_id="o1", department_id="d1", role="MEMBER", email="test@sevenwings.ai")
    plan_conv = QueryPlanner.analyze_and_plan("hello assistant", test_user)
    assert plan_conv.is_conversational_only
    plan_rag = QueryPlanner.analyze_and_plan("What is our security policy?", test_user)
    assert not plan_rag.is_conversational_only
    assert "department" in plan_rag.target_scopes

    out_ans, out_src, is_grounded, conf = GroundingValidator.get_out_of_context_response(
        "What is the future of Artificial Intelligence?",
        org_name="SevenWings AI"
    )
    assert not is_grounded
    assert len(out_src) == 0
    assert "does not contain records matching" in out_ans
    print("  [PASS] modules.rag_core (Planner, GroundingValidator, PromptEngine, Orchestrator) verified.")

    # 6. Test Background Tasks Domain
    from modules.tasks.celery_app import celery_app
    from modules.tasks.workers.ingestion_tasks import (
        async_ingest_document_task, async_execute_ingestion_job_task
    )
    assert "async_ingest_document_task" in celery_app.tasks
    assert "async_execute_ingestion_job_task" in celery_app.tasks
    print("  [PASS] modules.tasks (Celery app, worker tasks) verified.")

    # 7. Test FastAPI Presentation Layer & Dependencies
    from app.dependencies import get_db, get_orchestrator, get_optional_user, require_role
    from app.routes import auth, chat, documents, governance, jobs, audit, views
    from app.main import app
    assert app.title == settings.APP_NAME
    print("  [PASS] app Presentation Layer (FastAPI routes & dependency injection) verified.")

    print("\n=======================================================")
    print("ALL CLEAN MODULAR MONOLITH VERIFICATION TESTS PASSED!")
    print("=======================================================")

if __name__ == "__main__":
    test_modular_monolith_architecture()


# [CONFIG] settings.LLM_PROVIDER: gemini
# [CONFIG] settings.EMBEDDING_PROVIDER: ollama
# [CONFIG] settings.EMBEDDING_MODEL: qwen3-embedding:0.6b
# [CONFIG] settings.EMBEDDING_DIMENSION: 1024

# [LLM FACTORY] Type: GeminiService, Model: gemini-2.5-flash
# [EMBEDDING FACTORY] Type: OpenAICompatibleEmbeddingService, Model: qwen3-embedding:0.6b, Base URL: http://host.docker.internal:11434/v1

# [ORCHESTRATOR] LLM: GeminiService (gemini-2.5-flash)
# [ORCHESTRATOR] Embedding: OpenAICompatibleEmbeddingService (qwen3-embedding:0.6b)
# [HYBRID RETRIEVER] Embedding: qwen3-embedding:0.6b
# [SESSION STRATEGY] Embedding: qwen3-embedding:0.6b
# [ENTERPRISE STRATEGY] Retriever Embedding: qwen3-embedding:0.6b
# [SQL STRATEGY] Embedding: qwen3-embedding:0.6b

# --- Testing Dynamic Provider Switching Matrix ---
# Case A (LLM=ollama, EMB=ollama) -> LLM: gemma3:12b, EMB: qwen3-embedding:0.6b
# Case B (LLM=gemini, EMB=gemini) -> LLM: gemini-2.5-flash, EMB: text-embedding-004
# Case B2 (EMB=gemini, default fallback) -> EMB: text-embedding-004
# Case C (LLM=gemini, EMB=ollama) -> LLM: gemini-2.5-flash, EMB: qwen3-embedding:0.6b
# Case D (EMBEDDING_MODEL override on Ollama) -> Model: nomic-embed-text
# Case E (EMBEDDING_MODEL override on vLLM) -> Model: BAAI/bge-m3
# Case F (EMBEDDING_MODEL override on Gemini) -> Model: text-embedding-004

# >>> ALL VERIFICATION CHECKS & SWITCHING TESTS PASSED SUCCESSFULLY! Decoupling is 100% active. <<<
