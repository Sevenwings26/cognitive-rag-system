# benchmarking/test_multiturn_banking_scenario.py
import sys
import os
import uuid
import logging

BASE_DIR = "/home/techyz-admin/sevenwings/03_learning/26-08-10-ai-system/multi-tenant-rag-system"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.database import SessionLocal
from modules.auth.domain.models import User, Organization
from modules.auth.domain.tokens import TokenData
from modules.rag_core.orchestrator.unified_orchestrator import UnifiedRAGOrchestrator
from modules.rag_core.context import SessionContextManager
from modules.governance.repositories.chat_repository import ChatRepository

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_banking_multiturn")

def run_banking_multiturn_benchmark():
    db = SessionLocal()
    orchestrator = UnifiedRAGOrchestrator()

    try:
        print("\n" + "=" * 80)
        print("STARTING UNIVERSAL MULTI-TURN CONTEXT & ANAPHORA BENCHMARK")
        print("=" * 80)

        # 1. Resolve Super Admin User & Organization Context
        user = db.query(User).filter(User.email == "iarowosola@yahoo.com").first()
        if not user:
            user = db.query(User).first()
        assert user is not None, "Test user must exist"

        org = db.query(Organization).filter(Organization.id == user.org_id).first()
        assert org is not None, "Organization must exist"

        user_context = TokenData(
            user_id=user.id,
            email=user.email,
            role=user.role.value if hasattr(user.role, 'value') else str(user.role),
            org_id=org.id,
            department_id=user.department_id
        )

        session_id = f"test-multiturn-banking-{uuid.uuid4().hex[:8]}"
        session = ChatRepository.get_or_create_session(
            db=db,
            session_id=session_id,
            org_id=user_context.org_id,
            user_id=user_context.user_id,
            title="Banking Multi-Turn Benchmark"
        )
        print(f"Active Test Session ID: {session.id}")
        print(f"Tenant: {org.name} | User: {user.email}\n")

        # -------------------------------------------------------------------------
        # TURN 1: Customer Identity & Verification (noros_customer_db)
        # -------------------------------------------------------------------------
        print("--- [TURN 1] Customer Identity Lookup via BVN ---")
        q1 = "Who is the customer associated with Bank Verification Number (BVN) 90000001008?"
        print(f"User Prompt: {q1}")

        ChatRepository.add_message(db, session_id=session.id, role="user", content=q1)
        ans1, src1, grounded1, conf1 = orchestrator.execute_unified_query(
            query=q1,
            user_context=user_context,
            db=db,
            session_id=session.id
        )
        ChatRepository.add_message(db, session_id=session.id, role="assistant", content=ans1)

        print(f"AI Response:\n{ans1}\n")
        print(f"Sources: {[s.get('source_name') for s in src1]}")

        mem1 = SessionContextManager.get_memory(session.id)
        print(f"Working Memory after Turn 1:\n  Entities: {mem1.active_entities}\n  Names: {mem1.active_names}\n")

        assert "Adebayo" in ans1 or "Adekunle" in ans1, "Turn 1 must identify customer Adebayo Adekunle"
        assert "1008" in ans1 or "CUST-01008" in ans1, "Turn 1 must identify customer ID 1008"
        assert mem1.active_entities.get("customer_id") == 1008 or "customer_id" in mem1.active_entities, "Working memory must capture customer_id"
        print("[PASS] Turn 1 Verified Successfully.\n")

        # -------------------------------------------------------------------------
        # TURN 2: Account Discovery & Balances with Anaphora (noros_core_banking_db)
        # -------------------------------------------------------------------------
        print("--- [TURN 2] Account Discovery & Balances (Anaphora: 'he' -> Adebayo Adekunle) ---")
        q2 = "What bank accounts does he have with us, and what is his current total deposit balance?"
        print(f"User Prompt: {q2}")

        ChatRepository.add_message(db, session_id=session.id, role="user", content=q2)
        ans2, src2, grounded2, conf2 = orchestrator.execute_unified_query(
            query=q2,
            user_context=user_context,
            db=db,
            session_id=session.id
        )
        ChatRepository.add_message(db, session_id=session.id, role="assistant", content=ans2)

        print(f"AI Response:\n{ans2}\n")
        print(f"Sources: {[s.get('source_name') for s in src2]}")

        mem2 = SessionContextManager.get_memory(session.id)
        print(f"Working Memory after Turn 2:\n  Entities: {mem2.active_entities}\n  Metrics: {mem2.active_metrics}\n")

        assert "0123456708" in ans2, "Turn 2 must find corporate account 0123456708"
        assert "88,450,000" in ans2 or "88450000" in ans2, "Turn 2 must retrieve deposit balance 88,450,000"
        print("[PASS] Turn 2 Verified Successfully.\n")

        # -------------------------------------------------------------------------
        # TURN 3: Ledger Activity & Inflow Velocity (noros_core_banking_db)
        # -------------------------------------------------------------------------
        print("--- [TURN 3] Ledger Activity & Inflow Velocity ---")
        q3 = "Show me his transaction activity. What is his total inflow over the past 12 months, and what was his single largest credit deposit?"
        print(f"User Prompt: {q3}")

        ChatRepository.add_message(db, session_id=session.id, role="user", content=q3)
        ans3, src3, grounded3, conf3 = orchestrator.execute_unified_query(
            query=q3,
            user_context=user_context,
            db=db,
            session_id=session.id
        )
        ChatRepository.add_message(db, session_id=session.id, role="assistant", content=ans3)

        print(f"AI Response:\n{ans3}\n")
        print(f"Sources: {[s.get('source_name') for s in src3]}")

        mem3 = SessionContextManager.get_memory(session.id)
        print(f"Working Memory after Turn 3:\n  Entities: {mem3.active_entities}\n  Metrics: {mem3.active_metrics}\n")

        # Total inflow is 119,700,000.00, single largest deposit is 24,000,000.00
        assert "24,000,000" in ans3 or "24000000" in ans3 or "119,700,000" in ans3 or "119700000" in ans3, "Turn 3 must report transaction activity"
        print("[PASS] Turn 3 Verified Successfully.\n")

        # -------------------------------------------------------------------------
        # TURN 4: Credit Portfolio Cross-DB Pivot (noros_lending_db MSSQL 2022)
        # -------------------------------------------------------------------------
        print("--- [TURN 4] Cross-DB Pivot to MSSQL Lending (Anaphora: 'this same customer') ---")
        q4 = "Does this same customer hold any active loans or credit facilities in our lending system?"
        print(f"User Prompt: {q4}")

        ChatRepository.add_message(db, session_id=session.id, role="user", content=q4)
        ans4, src4, grounded4, conf4 = orchestrator.execute_unified_query(
            query=q4,
            user_context=user_context,
            db=db,
            session_id=session.id
        )
        ChatRepository.add_message(db, session_id=session.id, role="assistant", content=ans4)

        print(f"AI Response:\n{ans4}\n")
        print(f"Sources: {[s.get('source_name') for s in src4]}")

        mem4 = SessionContextManager.get_memory(session.id)
        print(f"Working Memory after Turn 4:\n  Entities: {mem4.active_entities}\n  Metrics: {mem4.active_metrics}\n")

        assert "LN-1008" in ans4 or "3008" in ans4 or "45,000,000" in ans4, "Turn 4 must find loan LN-1008"
        print("[PASS] Turn 4 Verified Successfully.\n")

        print("=" * 80)
        print("ALL 4 PROGRESSIVE MULTI-TURN BENCHMARK TURNS PASSED 100%!")
        print("=" * 80)

    finally:
        db.close()

if __name__ == "__main__":
    run_banking_multiturn_benchmark()
