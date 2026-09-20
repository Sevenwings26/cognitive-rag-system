# benchmarking/test_multiturn_enterprise_drift.py
import sys
import os
import uuid
import logging

BASE_DIR = "/app"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.database import SessionLocal
from modules.auth.domain.models import User, Organization, UserRole
from modules.auth.domain.tokens import TokenData
from modules.rag_core.orchestrator.unified_orchestrator import UnifiedRAGOrchestrator
from modules.rag_core.context import SessionContextManager, EntityScope
from modules.governance.repositories.chat_repository import ChatRepository

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_multiturn_drift")

def run_enterprise_drift_benchmark():
    db = SessionLocal()
    orchestrator = UnifiedRAGOrchestrator()

    try:
        print("\n" + "=" * 90)
        print("STARTING 8-TURN ENTERPRISE CONTEXT DRIFT & ANTI-COLLAPSE BENCHMARK")
        print("Target: Tier-1 Conglomerates, Commercial Banks, Central Banks")
        print("=" * 90)

        # 1. Resolve Super Admin User & Org
        user = db.query(User).filter(User.email == "iarowosola@yahoo.com").first() or db.query(User).first()
        assert user is not None, "Test user must exist"

        org = db.query(Organization).filter(Organization.id == user.org_id).first()
        assert org is not None, "Organization must exist"

        admin_token = TokenData(
            user_id=user.id,
            email=user.email,
            role=user.role.value if hasattr(user.role, 'value') else str(user.role),
            org_id=org.id,
            department_id=user.department_id
        )

        session_id = f"test-enterprise-drift-{uuid.uuid4().hex[:8]}"
        session = ChatRepository.get_or_create_session(
            db=db,
            session_id=session_id,
            org_id=admin_token.org_id,
            user_id=admin_token.user_id,
            title="Enterprise Multi-Turn Anti-Drift Benchmark"
        )
        print(f"Session ID: {session.id}")
        print(f"Tenant: {org.name} | User: {user.email} (Role: {admin_token.role})\n")

        # -------------------------------------------------------------------------
        # TURN 1: Identity Lookup via BVN (noros_customer_db)
        # -------------------------------------------------------------------------
        print("-" * 80)
        print(">>> [TURN 1] Customer Identity Lookup via BVN")
        q1 = "Who is the customer associated with Bank Verification Number (BVN) 90000001008?"
        print(f"Prompt: {q1}")

        ChatRepository.add_message(db, session_id=session.id, role="user", content=q1)
        ans1, src1, _, _ = orchestrator.execute_unified_query(q1, admin_token, db=db, session_id=session.id)
        ChatRepository.add_message(db, session_id=session.id, role="assistant", content=ans1)
        print(f"Answer:\n{ans1}")
        mem1 = SessionContextManager.get_memory(session.id)
        print(f"Memory: entities={mem1.active_entities}, names={mem1.active_names}, scope={mem1.scope}\n")

        assert "Adebayo" in ans1 or "Adekunle" in ans1, "Turn 1 must resolve Adebayo Adekunle"
        assert "1008" in ans1 or "CUST-01008" in ans1, "Turn 1 must resolve Customer ID 1008"
        print("[PASS] Turn 1 Verified: Single customer identity established.")

        # -------------------------------------------------------------------------
        # TURN 2: Employer & Address (noros_customer_db)
        # -------------------------------------------------------------------------
        print("-" * 80)
        print(">>> [TURN 2] Employer and Address Lookup with Anaphora ('his')")
        q2 = "What is his employer and branch address?"
        print(f"Prompt: {q2}")

        ChatRepository.add_message(db, session_id=session.id, role="user", content=q2)
        ans2, src2, _, _ = orchestrator.execute_unified_query(q2, admin_token, db=db, session_id=session.id)
        ChatRepository.add_message(db, session_id=session.id, role="assistant", content=ans2)
        print(f"Answer:\n{ans2}")
        mem2 = SessionContextManager.get_memory(session.id)
        print(f"Memory: entities={mem2.active_entities}, names={mem2.active_names}, scope={mem2.scope}\n")

        assert "Dangote" in ans2, "Turn 2 must find employer Dangote"
        print("[PASS] Turn 2 Verified: Anaphora resolved to Dangote employer.")

        # -------------------------------------------------------------------------
        # TURN 3: Aggregation Filter: Other customers employed by Dangote
        # -------------------------------------------------------------------------
        print("-" * 80)
        print(">>> [TURN 3] Filter Other Customers Employed by Dangote")
        q3 = "Are there other customers employed by Dangote?"
        print(f"Prompt: {q3}")

        ChatRepository.add_message(db, session_id=session.id, role="user", content=q3)
        ans3, src3, _, _ = orchestrator.execute_unified_query(q3, admin_token, db=db, session_id=session.id)
        ChatRepository.add_message(db, session_id=session.id, role="assistant", content=ans3)
        print(f"Answer:\n{ans3}")
        mem3 = SessionContextManager.get_memory(session.id)
        print(f"Memory: entities={mem3.active_entities}, names={mem3.active_names}, scope={mem3.scope}\n")

        assert "1001" in ans3 or "1021" in ans3 or "1041" in ans3 or "Yakubu" in ans3 or "Rukayat" in ans3, "Turn 3 must return other Dangote employees"
        assert mem3.scope == EntityScope.COLLECTION.value, "Turn 3 scope must transition to COLLECTION"
        print("[PASS] Turn 3 Verified: Successfully retrieved collection of employees without memory poisoning.")

        # -------------------------------------------------------------------------
        # TURN 4: Multi-entity drill down: Dangote employees and addresses
        # -------------------------------------------------------------------------
        print("-" * 80)
        print(">>> [TURN 4] List Dangote Employees and Branch Addresses")
        q4 = "List the Dangote employees and their branch addresses."
        print(f"Prompt: {q4}")

        ChatRepository.add_message(db, session_id=session.id, role="user", content=q4)
        ans4, src4, _, _ = orchestrator.execute_unified_query(q4, admin_token, db=db, session_id=session.id)
        ChatRepository.add_message(db, session_id=session.id, role="assistant", content=ans4)
        print(f"Answer:\n{ans4}")
        mem4 = SessionContextManager.get_memory(session.id)
        print(f"Memory: entities={mem4.active_entities}, scope={mem4.scope}\n")

        assert "Dangote" in ans4 or "1008" in ans4 or "1001" in ans4, "Turn 4 must list Dangote employees"
        print("[PASS] Turn 4 Verified: Collection query processed.")

        # -------------------------------------------------------------------------
        # TURN 5: Semantic Schema Disambiguation & Topic Shift
        # -------------------------------------------------------------------------
        print("-" * 80)
        print(">>> [TURN 5] Semantic Schema Disambiguation ('industries' -> employer NOT occupation)")
        q5 = "Apart from dangote, which other industries do we have customers from?"
        print(f"Prompt: {q5}")

        ChatRepository.add_message(db, session_id=session.id, role="user", content=q5)
        ans5, src5, _, _ = orchestrator.execute_unified_query(q5, admin_token, db=db, session_id=session.id)
        ChatRepository.add_message(db, session_id=session.id, role="assistant", content=ans5)
        print(f"Answer:\n{ans5}")
        mem5 = SessionContextManager.get_memory(session.id)
        print(f"Memory: entities={mem5.active_entities}, scope={mem5.scope}\n")

        # Must NOT return personal professions like "Petroleum Geologist" or "Software Engineer" as industries
        # Must return actual corporate employers/industries like Julius Berger, Unilever, NNPC, Access Holdings, etc.
        assert not any(job in ans5 for job in ["Petroleum Geologist", "Software Engineer"]), "Turn 5 must NOT return job titles (occupation) for industries!"
        corporate_found = any(corp in ans5 for corp in ["Julius Berger", "Unilever", "GrainBelt", "Access", "Finance", "Breweries", "KPMG", "Zenith", "MTN"])
        assert corporate_found, f"Turn 5 must return corporate employers/industries from employer column. Got:\n{ans5}"
        print("[PASS] Turn 5 Verified: Successfully queried employer column instead of occupation.")

        # -------------------------------------------------------------------------
        # TURN 6: Strategy Routing Protection (Organization serving -> SQL, NOT RAG)
        # -------------------------------------------------------------------------
        print("-" * 80)
        print(">>> [TURN 6] Strategy Routing Protection ('Which other organization are we serving?')")
        q6 = "Which other organization are we serving?"
        print(f"Prompt: {q6}")

        ChatRepository.add_message(db, session_id=session.id, role="user", content=q6)
        ans6, src6, _, _ = orchestrator.execute_unified_query(q6, admin_token, db=db, session_id=session.id)
        ChatRepository.add_message(db, session_id=session.id, role="assistant", content=ans6)
        print(f"Answer:\n{ans6}")
        print(f"Sources: {[s.get('source_name') for s in src6]}")
        mem6 = SessionContextManager.get_memory(session.id)
        print(f"Memory: entities={mem6.active_entities}, scope={mem6.scope}\n")

        # Verify zero context bleed: must NOT cite NutanixBible.pdf, Book.xlsx, project_dataset_cleaned.xlsx
        leaked_files = [s.get('filename') for s in src6 if s.get('filename') in ('NutanixBible.pdf', 'Book.xlsx', 'project_dataset_cleaned.xlsx')]
        assert not leaked_files, f"Turn 6 failed: Leaked document RAG files: {leaked_files}"
        assert any(corp in ans6 for corp in ["Julius Berger", "Unilever", "GrainBelt", "Access", "Dangote", "KPMG", "Zenith", "MTN"]), "Turn 6 must return organizations served from customers database"
        print("[PASS] Turn 6 Verified: Routed to Dynamic SQL with zero document context bleed.")

        # -------------------------------------------------------------------------
        # TURN 7: Vocabulary Sensitivity & Exclusion
        # -------------------------------------------------------------------------
        print("-" * 80)
        print(">>> [TURN 7] Exclusion & Anaphora ('Which other organization, apart from dangote are we serving?')")
        q7 = "Which other organization, apart from dangote are we serving?"
        print(f"Prompt: {q7}")

        ChatRepository.add_message(db, session_id=session.id, role="user", content=q7)
        ans7, src7, _, _ = orchestrator.execute_unified_query(q7, admin_token, db=db, session_id=session.id)
        ChatRepository.add_message(db, session_id=session.id, role="assistant", content=ans7)
        print(f"Answer:\n{ans7}")
        mem7 = SessionContextManager.get_memory(session.id)
        print(f"Memory: entities={mem7.active_entities}, scope={mem7.scope}\n")

        assert any(corp in ans7 for corp in ["Julius Berger", "Unilever", "GrainBelt", "Access", "KPMG", "Zenith", "MTN"]), "Turn 7 must return non-Dangote organizations"
        print("[PASS] Turn 7 Verified: Exclusionary organization query executed.")

        # -------------------------------------------------------------------------
        # TURN 8A: Anti-Pollution & System Meta-Query Trap (SUPER_ADMIN)
        # -------------------------------------------------------------------------
        print("-" * 80)
        print(">>> [TURN 8A] System Meta-Query Trap (SUPER_ADMIN: Full Authoritative Inventory)")
        q8 = "How many data sources do we have on this system?"
        print(f"Prompt: {q8}")

        ChatRepository.add_message(db, session_id=session.id, role="user", content=q8)
        ans8, src8, _, _ = orchestrator.execute_unified_query(q8, admin_token, db=db, session_id=session.id)
        ChatRepository.add_message(db, session_id=session.id, role="assistant", content=ans8)
        print(f"Answer:\n{ans8}")
        print(f"Sources: {[s.get('source_name') for s in src8]}")
        mem8 = SessionContextManager.get_memory(session.id)
        print(f"Memory: entities={mem8.active_entities}, scope={mem8.scope}\n")

        # Verify NO customer_id 1041 or BVN 90000001008 hallucinated in answer
        assert "1041" not in ans8, "Turn 8 must NOT contain hallucinated customer_id 1041!"
        assert "90000001008" not in ans8, "Turn 8 must NOT contain hallucinated BVN!"
        assert "7" in ans8 or "noros_customer_db" in ans8 or "Sharepoint Source" in ans8, "Turn 8 must list registered enterprise sources"
        print("[PASS] Turn 8A Verified: Super admin received authoritative platform inventory with zero entity pollution.")

        # -------------------------------------------------------------------------
        # TURN 8B: RBAC Security Guard for Non-Admin (MEMBER)
        # -------------------------------------------------------------------------
        print("-" * 80)
        print(">>> [TURN 8B] RBAC Security Guard (MEMBER: Information Disclosure Prevention)")
        member_token = TokenData(
            user_id="member-user-123",
            email="member@enterprise.com",
            role="MEMBER",
            org_id=org.id,
            department_id=None
        )

        ans8_member, src8_member, _, _ = orchestrator.execute_unified_query(q8, member_token, db=db, session_id=session.id)
        print(f"Member Answer:\n{ans8_member}")

        assert "restricted" in ans8_member.lower() or "security" in ans8_member.lower(), "Turn 8B must return governance restriction message to non-admins"
        assert "noros_customer_db" not in ans8_member, "Turn 8B must NEVER disclose internal database names to non-admins!"
        print("[PASS] Turn 8B Verified: Non-admin query safely blocked by RBAC governance.")

        print("\n" + "=" * 90)
        print("ALL 8 PROGRESSIVE ENTERPRISE TURNS PASSED 100% WITH ZERO DRIFT & FULL RBAC!")
        print("=" * 90)

    finally:
        db.close()

if __name__ == "__main__":
    run_enterprise_drift_benchmark()
