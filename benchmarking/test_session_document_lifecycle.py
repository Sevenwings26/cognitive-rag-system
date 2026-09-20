# benchmarking/test_session_document_lifecycle.py
import sys
import os
import time
import uuid
import requests

BASE_DIR = "/home/techyz-admin/sevenwings/03_learning/26-08-10-ai-system/multi-tenant-rag-system"
sys.path.insert(0, BASE_DIR)

from core.database import SessionLocal
from modules.auth.domain.models import User, Organization, Department, UserRole
from modules.auth.services.token_service import create_access_token, get_password_hash
from modules.governance.domain.models import EnterpriseDocument, DocumentChunk, ChatSession
from modules.governance.repositories.document_repository import DocumentRepository
from modules.governance.repositories.chat_repository import ChatRepository
from modules.rag_core.retrieval.vector_store import VectorStoreService
from qdrant_client.models import Filter, FieldCondition, MatchValue

API_BASE = "http://localhost:4500"
PDF_CANDIDATES = [
    "/app/benchmarking/BASWE - 100 AI Engineering Interview Questions.pdf",
    "/home/techyz-admin/sevenwings/03_learning/26-08-10-ai-system/multi-tenant-rag-system/benchmarking/BASWE - 100 AI Engineering Interview Questions.pdf",
    "/mnt/c/Users/Admin/Downloads/BASWE - 100 AI Engineering Interview Questions.pdf"
]
PDF_PATH = next((p for p in PDF_CANDIDATES if os.path.exists(p)), PDF_CANDIDATES[0])

def run_lifecycle_test():
    print("=" * 70)
    print("RUNNING SESSION DOCUMENT LIFECYCLE & ZERO-BLEED RETRIEVAL BENCHMARK")
    print("=" * 70)

    assert os.path.exists(PDF_PATH), f"Test PDF not found at {PDF_PATH}"
    print(f"  [OK] Found test PDF: {PDF_PATH} ({os.path.getsize(PDF_PATH)} bytes)")

    db = SessionLocal()
    vs = VectorStoreService()

    # 1. Setup Test Organization and Authenticated User
    org = db.query(Organization).filter(Organization.slug == "test-lifecycle-org").first()
    if not org:
        org = Organization(id=str(uuid.uuid4()), name="Test Lifecycle Org", slug="test-lifecycle-org")
        db.add(org)
        db.commit()

    test_user = db.query(User).filter(User.email == "test_session_user@lifecycle.local").first()
    if not test_user:
        test_user = User(
            id=str(uuid.uuid4()),
            org_id=org.id,
            email="test_session_user@lifecycle.local",
            hashed_password=get_password_hash("testpass123"),
            role=UserRole.MEMBER,
            full_name="Lifecycle Test User"
        )
        db.add(test_user)
        db.commit()

    token = create_access_token(
        data={
            "sub": test_user.id,
            "email": test_user.email,
            "org_id": org.id,
            "role": test_user.role.value,
            "department_id": ""
        }
    )
    headers = {"Authorization": f"Bearer {token}"}
    print(f"  [OK] Test user authenticated: {test_user.email} (org: {org.name})")

    created_session_id = None
    uploaded_doc_id = None

    try:
        # -------------------------------------------------------------
        # STEP 1: Upload PDF to /chat/upload without initial session_id
        # -------------------------------------------------------------
        print("\n[STEP 1] Testing /chat/upload with NO session_id (Draft State)...")
        with open(PDF_PATH, "rb") as f:
            files = {"file": (os.path.basename(PDF_PATH), f, "application/pdf")}
            resp = requests.post(f"{API_BASE}/chat/upload", files=files, headers=headers)

        assert resp.status_code == 202, f"Expected 202, got {resp.status_code}: {resp.text}"
        upload_data = resp.json()
        print(f"  Response: {upload_data}")

        assert upload_data["status"] == "accepted"
        created_session_id = upload_data.get("session_id")
        uploaded_doc_id = upload_data.get("document_id")
        session_title = upload_data.get("session_title")

        assert created_session_id, "Expected non-null session_id in upload response"
        assert uploaded_doc_id, "Expected non-null document_id in upload response"
        assert session_title, "Expected non-null session_title in upload response"
        print(f"  [PASS] Auto-provisioned session: id='{created_session_id}', title='{session_title}'")

        # Verify chat_session exists in DB
        chat_sess = db.query(ChatSession).filter(ChatSession.id == created_session_id).first()
        assert chat_sess is not None, f"Chat session {created_session_id} not found in DB"
        print(f"  [PASS] Chat session verified in PostgreSQL.")

        # -------------------------------------------------------------
        # STEP 2: Poll Document Indexing Status
        # -------------------------------------------------------------
        print("\n[STEP 2] Waiting for document background indexing to complete...")
        indexed = False
        chunk_count = 0
        for attempt in range(25):
            status_resp = requests.get(f"{API_BASE}/documents/{uploaded_doc_id}/status", headers=headers)
            assert status_resp.status_code == 200, f"Status check failed: {status_resp.text}"
            s_data = status_resp.json()
            status_val = s_data.get("status")
            chunk_count = s_data.get("chunk_count", 0)
            print(f"    Attempt {attempt + 1}: status={status_val}, chunks={chunk_count}")

            if status_val == "INDEXED":
                indexed = True
                break
            elif status_val == "FAILED":
                raise RuntimeError(f"Ingestion failed: {s_data.get('error_message')}")
            time.sleep(1.5)

        assert indexed, "Document indexing did not complete within timeout"
        assert chunk_count > 0, f"Expected chunk_count > 0, got {chunk_count}"
        print(f"  [PASS] Document successfully indexed with {chunk_count} chunks.")

        # -------------------------------------------------------------
        # STEP 3: Verify Qdrant Points Payload Tagging
        # -------------------------------------------------------------
        print("\n[STEP 3] Verifying Qdrant Vector Payload Scoping & Session Binding...")
        session_filter = Filter(
            must=[
                FieldCondition(key="org_id", match=MatchValue(value=org.id)),
                FieldCondition(key="session_id", match=MatchValue(value=created_session_id)),
                FieldCondition(key="scope", match=MatchValue(value="session"))
            ]
        )
        count_res = vs.client.count(collection_name=vs.collection_name, count_filter=session_filter)
        print(f"  Qdrant points matching (org_id, session_id='{created_session_id}', scope='session'): {count_res.count}")
        assert count_res.count == chunk_count, f"Expected {chunk_count} points in Qdrant, got {count_res.count}"
        print("  [PASS] All chunks correctly indexed with scope='session' and session_id bound.")

        # -------------------------------------------------------------
        # STEP 4: Query Session Document ("What is the document about?")
        # -------------------------------------------------------------
        print("\n[STEP 4] Querying in Session: 'What is the document about?'...")
        query_payload = {
            "query": "What is the document about?",
            "session_id": created_session_id
        }
        query_resp = requests.post(f"{API_BASE}/chat/query", json=query_payload, headers=headers)
        assert query_resp.status_code == 200, f"Query failed: {query_resp.text}"
        query_data = query_resp.json()

        print(f"\nAI Assistant Answer:\n{query_data['answer']}\n")
        sources = query_data.get("sources", [])
        print(f"Sources cited ({len(sources)}):")
        for s in sources:
            print(f"  - {s.get('filename')} (score: {s.get('relevance_score')})")

        assert len(sources) > 0, "Expected at least 1 source citation from the uploaded document"
        for s in sources:
            fname = s.get("filename")
            assert fname == os.path.basename(PDF_PATH), f"Unexpected source leaked: {fname}"

        # Ensure no enterprise context bleed
        leaked_files = [s["filename"] for s in sources if s["filename"] in ["NutanixBible.pdf", "Book.xlsx", "approved_vendors.xlsx"]]
        assert len(leaked_files) == 0, f"Context bleed detected! Enterprise files returned: {leaked_files}"
        print("  [PASS] Zero context bleed! Sources cited exclusively from uploaded session document.")

        # Check content of answer
        answer_lower = query_data["answer"].lower()
        has_relevant_info = any(term in answer_lower for term in [
            "interview", "engineering", "question", "module", "rag", "llm", "accelerator"
        ])
        assert has_relevant_info, f"Answer does not appear to describe the AI interview guide: {query_data['answer']}"
        print("  [PASS] Assistant accurately and fluently described the AI Engineering Interview Questions document.")

        # -------------------------------------------------------------
        # STEP 5: Cross-Session Zero-Bleed Isolation Test
        # -------------------------------------------------------------
        print("\n[STEP 5] Testing Cross-Session Isolation (Query in an Unrelated Session)...")
        other_session = ChatRepository.get_or_create_session(
            db=db,
            session_id=None,
            org_id=org.id,
            user_id=test_user.id,
            title="Unrelated Session"
        )
        cross_query_payload = {
            "query": "What is the document about?",
            "session_id": other_session.id
        }
        cross_resp = requests.post(f"{API_BASE}/chat/query", json=cross_query_payload, headers=headers)
        assert cross_resp.status_code == 200, f"Cross query failed: {cross_resp.text}"
        cross_data = cross_resp.json()

        cross_sources = cross_data.get("sources", [])
        print(f"Cross-session sources cited: {[s.get('filename') for s in cross_sources]}")
        for s in cross_sources:
            assert s.get("filename") != os.path.basename(PDF_PATH), "Cross-session bleed! BASWE doc leaked into other session!"
        print("  [PASS] Cross-session isolation verified: BASWE document is inaccessible from other sessions.")

        # -------------------------------------------------------------
        # STEP 6: Enterprise Search Zero-Bleed Test
        # -------------------------------------------------------------
        print("\n[STEP 6] Testing Enterprise Knowledge Search Zero-Bleed Isolation...")
        ent_query_payload = {
            "query": "What are the 100 AI Engineering Interview Questions?",
            "session_id": other_session.id,
            "scope": "enterprise"
        }
        ent_resp = requests.post(f"{API_BASE}/chat/query", json=ent_query_payload, headers=headers)
        assert ent_resp.status_code == 200, f"Enterprise query failed: {ent_resp.status_code}: {ent_resp.text}"
        ent_data = ent_resp.json()
        ent_sources = ent_data.get("sources", [])
        for s in ent_sources:
            assert s.get("filename") != os.path.basename(PDF_PATH), "Enterprise bleed! Session doc leaked into enterprise RAG!"
        print("  [PASS] Enterprise search isolation verified: Session doc excluded from enterprise knowledge queries.")

        # -------------------------------------------------------------
        # STEP 7: Session Cleanup & Vector Purge Verification
        # -------------------------------------------------------------
        print("\n[STEP 7] Testing Session Deletion & Vector Cleanup...")
        del_resp = requests.delete(f"{API_BASE}/chat/{created_session_id}", headers=headers)
        assert del_resp.status_code == 200, f"Delete failed: {del_resp.text}"

        post_delete_count = vs.client.count(collection_name=vs.collection_name, count_filter=session_filter)
        assert post_delete_count.count == 0, f"Expected 0 vectors after session delete, got {post_delete_count.count}"
        print("  [PASS] Session vectors successfully purged from Qdrant upon session deletion.")

    finally:
        if uploaded_doc_id:
            try:
                DocumentRepository.delete_document(db, uploaded_doc_id, org.id)
            except Exception:
                pass
        db.close()

    print("\n" + "=" * 70)
    print("ALL 7 SESSION DOCUMENT LIFECYCLE & ZERO-BLEED TESTS PASSED!")
    print("=" * 70)

if __name__ == "__main__":
    run_lifecycle_test()
