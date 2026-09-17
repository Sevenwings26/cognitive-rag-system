# benchmarking/test_orchestrator_pipeline.py
import sys
import os
import uuid
import logging

BASE_DIR = "/home/techyz-admin/sevenwings/03_learning/26-08-10-ai-system/multi-tenant-rag-system"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.database import SessionLocal
from modules.auth.domain.models import User, Organization, Department, UserRole
from modules.auth.domain.tokens import TokenData
from modules.rag_core.orchestrator.unified_orchestrator import UnifiedRAGOrchestrator
from modules.rag_core.orchestrator.query_planner import QueryPlanner

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_pipeline")

def run_tests():
    db = SessionLocal()
    orchestrator = UnifiedRAGOrchestrator()

    try:
        print("\n" + "=" * 80)
        print("STARTING RETRIEVAL PIPELINE TEST SUITE (3 EXECUTION PATHS)")
        print("=" * 80)

        # 1. Resolve Super Admin User & Organization Context
        user = db.query(User).filter(User.email == "iarowosola@yahoo.com").first()
        if not user:
            user = db.query(User).first()
        assert user is not None, "At least one User must exist in the database"

        org = db.query(Organization).filter(Organization.id == user.org_id).first()
        assert org is not None, "Organization for user must exist"

        user_context = TokenData(
            user_id=user.id,
            email=user.email,
            role=user.role.value if hasattr(user.role, 'value') else str(user.role),
            org_id=org.id,
            department_id=user.department_id
        )

        print(f"Test Tenant Context: Org='{org.name}' ({org.id}), User='{user.email}' (Role: {user_context.role})")

        # -------------------------------------------------------------------------
        # TEST PATH 1: UNGROUNDED GENERAL CHAT ROUTE (Route A)
        # -------------------------------------------------------------------------
        print("\n--- [PATH 1] UNGROUNDED GENERAL CONVERSATIONAL ROUTE ---")
        general_query = "Hello, who are you and what can you help me with?"
        
        # Verify QueryPlanner intent classification
        plan1 = QueryPlanner.analyze_and_plan(query=general_query, user_context=user_context, mode="auto")
        print(f"  QueryPlanner intent: {plan1.intent_category}, is_conversational_only: {plan1.is_conversational_only}")
        assert plan1.is_conversational_only is True, f"Expected is_conversational_only=True, got {plan1.is_conversational_only}"

        # Execute query via orchestrator
        answer1, sources1, is_grounded1, confidence1 = orchestrator.execute_unified_query(
            query=general_query,
            user_context=user_context,
            db=db,
            mode="auto"
        )
        print(f"  Response Answer: {answer1[:120]}...")
        print(f"  Sources Count: {len(sources1)} (Expected: 0)")
        print(f"  Grounding Flag: {is_grounded1}, Confidence: {confidence1}")
        assert len(sources1) == 0, f"Conversational query should return 0 sources, got {len(sources1)}"
        assert is_grounded1 is True, "Conversational query should return grounded=True"
        assert len(answer1) > 20, "Answer should be non-empty"
        print("  [PASS] Path 1 (General Conversational) verified successfully.")

        # -------------------------------------------------------------------------
        # TEST PATH 2: ENTERPRISE KNOWLEDGE RETRIEVAL (Route C - Enterprise Scope)
        # -------------------------------------------------------------------------
        print("\n--- [PATH 2] ENTERPRISE KNOWLEDGE RETRIEVAL ROUTE ---")
        enterprise_query = "What is our company expense reimbursement policy or employee handbook procedure?"
        
        plan2 = QueryPlanner.analyze_and_plan(query=enterprise_query, user_context=user_context, mode="auto")
        print(f"  QueryPlanner intent: {plan2.intent_category}, is_conversational_only: {plan2.is_conversational_only}")
        assert plan2.is_conversational_only is False, "Enterprise query should not be conversational only"

        answer2, sources2, is_grounded2, confidence2 = orchestrator.execute_unified_query(
            query=enterprise_query,
            user_context=user_context,
            db=db,
            mode="rag"
        )
        print(f"  Response Answer: {answer2[:200]}...")
        print(f"  Sources Count: {len(sources2)}")
        for idx, s in enumerate(sources2, 1):
            print(f"    - Source {idx}: {s.get('filename')} (relevance: {s.get('relevance_score')})")
        print(f"  Grounding Flag: {is_grounded2}, Confidence: {confidence2}")
        assert len(answer2) > 20, "RAG answer should be non-empty"
        assert len(sources2) > 0, "Enterprise knowledge query should return relevant sources"
        print("  [PASS] Path 2 (Enterprise RAG) verified successfully.")

        # -------------------------------------------------------------------------
        # TEST PATH 3: SCOPED IN-CHAT DOCUMENT QUERY (Route C - Session Scope)
        # -------------------------------------------------------------------------
        print("\n--- [PATH 3] SCOPED IN-CHAT DOCUMENT QUERY (SESSION ISOLATION) ---")
        test_session_id = f"test-session-{uuid.uuid4().hex[:8]}"
        other_session_id = f"other-session-{uuid.uuid4().hex[:8]}"
        doc_id = str(uuid.uuid4())
        
        session_doc_content = (
            "CONFIDENTIAL IN-CHAT DOCUMENT FOR SESSION ONLY.\n"
            "Project Codename: PROJECT_AURORA_2026.\n"
            "Budget Allocation: Exactly $875,000 USD approved for Cloud Infrastructure migration.\n"
            "Key Lead: Chief Architect Marcus Thorne."
        )
        
        print(f"  Ingesting session-scoped document into session_id='{test_session_id}'...")
        chunk_count = orchestrator.ingest_document(
            filename="session_aurora_brief.txt",
            file_bytes=session_doc_content.encode("utf-8"),
            document_id=doc_id,
            org_id=org.id,
            department_id=user.department_id,
            uploader_id=user.id,
            access_level="DEPARTMENT",
            session_id=test_session_id,
            mime_type="text/plain",
            db=db
        )
        print(f"  Successfully ingested {chunk_count} chunk(s) for session '{test_session_id}'.")

        # Query targeting this specific session
        session_query = "What is the project codename and approved budget allocation?"
        answer3, sources3, is_grounded3, confidence3 = orchestrator.execute_unified_query(
            query=session_query,
            user_context=user_context,
            db=db,
            session_id=test_session_id,
            scope="session",
            mode="rag"
        )
        print(f"  Session Query Answer: {answer3}")
        print(f"  Sources Count: {len(sources3)}")
        for s in sources3:
            print(f"    - Filename: {s.get('filename')}, Doc ID: {s.get('document_id')}")

        assert len(sources3) > 0, "Session scoped query must retrieve the session document"
        assert sources3[0]["filename"] == "session_aurora_brief.txt", "Source must match session document"
        assert "AURORA" in answer3.upper() or "875,000" in answer3 or "MARCUS" in answer3.upper(), "Answer must contain session facts"
        print("  [PASS] Session document retrieval succeeded with grounded facts.")

        # Cross-Session Leakage Check:
        # Query using a DIFFERENT session_id with scope="session" - MUST NOT retrieve aurora brief!
        print(f"  Verifying strict boundary: querying with different session_id='{other_session_id}'...")
        answer_cross, sources_cross, _, _ = orchestrator.execute_unified_query(
            query=session_query,
            user_context=user_context,
            db=db,
            session_id=other_session_id,
            scope="session",
            mode="rag"
        )
        print(f"  Cross-session Sources Count: {len(sources_cross)} (Expected: 0)")
        assert len(sources_cross) == 0, f"Security violation: Chunks leaked across sessions! Found: {sources_cross}"
        print("  [PASS] Cross-session boundary strictly enforced (0 chunks leaked).")

        # Cleanup: Delete session vectors
        print(f"  Cleaning up session vectors for '{test_session_id}'...")
        orchestrator.delete_session_vectors(test_session_id)
        orchestrator.delete_document_vectors(doc_id, org.id, db=db)
        print("  [PASS] Session vectors and test chunks cleaned up successfully.")

        print("\n" + "=" * 80)
        print("ALL 3 RETRIEVAL PIPELINE EXECUTION PATHS VERIFIED SUCCESSFULLY!")
        print("=" * 80)

    finally:
        db.close()

if __name__ == "__main__":
    run_tests()
