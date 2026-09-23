# benchmarking/test_action_proposer.py
import sys
import os
import uuid
import logging
from typing import List, Dict, Any

BASE_DIR = "/home/techyz-admin/sevenwings/03_learning/26-08-10-ai-system/multi-tenant-rag-system"
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.database import SessionLocal
from modules.auth.domain.models import User, Organization
from modules.auth.domain.tokens import TokenData
from modules.rag_core.orchestrator.unified_orchestrator import UnifiedRAGOrchestrator
from modules.rag_core.context import (
    SessionContextManager,
    SessionWorkingMemory,
    EntityScope,
    ActionType,
    SuggestedAction,
    ActionProposer
)
from modules.governance.repositories.chat_repository import ChatRepository

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("test_action_proposer")

def run_action_proposer_benchmark():
    db = SessionLocal()
    orchestrator = UnifiedRAGOrchestrator()

    try:
        print("\n" + "=" * 80)
        print("STARTING PROACTIVE AGENT COGNITION & NEXT BEST ACTIONS BENCHMARK")
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
            org_id=org.id,
            role=user.role,
            department_id=user.department_id
        )

        session_id = f"test-proactive-{uuid.uuid4().hex[:8]}"
        session = ChatRepository.get_or_create_session(
            db=db,
            session_id=session_id,
            org_id=user_context.org_id,
            user_id=user_context.user_id,
            title="Proactive Actions Benchmark"
        )
        print(f"Active Session ID: {session.id}")
        print(f"Tenant: {org.name} | User: {user.email}\n")

        # -------------------------------------------------------------------------
        # TURN 1: Customer Identity Lookup via BVN (noros_customer_db)
        # -------------------------------------------------------------------------
        print("--- [TURN 1] Customer Identity Lookup via BVN ---")
        q1 = "Who is the customer associated with Bank Verification Number (BVN) 90000001008?"
        print(f"Prompt: {q1}")

        ChatRepository.add_message(db, session_id=session.id, role="user", content=q1)
        ans1, src1, grounded1, conf1, actions1 = orchestrator.execute_unified_query(
            query=q1,
            user_context=user_context,
            db=db,
            session_id=session.id,
            return_actions=True
        )
        ChatRepository.add_message(db, session_id=session.id, role="assistant", content=ans1)

        print(f"AI Response:\n{ans1[:250]}...\n")
        print(f"Suggested Actions Turn 1 ({len(actions1)} actions):")
        for a in actions1:
            print(f"  - [{a.get('action_type')}] {a.get('label')}: \"{a.get('suggested_prompt')}\" (conversational: \"{a.get('conversational_prompt')}\")")

        # Assertions for Turn 1:
        assert len(actions1) >= 2, "Turn 1 must propose at least 2 next best actions"
        labels1 = [a.get("label", "").lower() for a in actions1]
        assert any("account" in lbl for lbl in labels1), "Turn 1 must propose viewing deposit accounts"
        assert any("loan" in lbl for lbl in labels1), "Turn 1 must propose checking loan portfolio"
        assert all(a.get("conversational_prompt") for a in actions1), "All actions must have conversational_prompt"
        assert all(a.get("conversational_prompt", "").endswith("?") for a in actions1), "Conversational prompts must end with '?'"
        assert "**Next steps you might consider:**" in ans1, "Turn 1 response must embed consultative recommendations block"

        mem1 = SessionContextManager.get_memory(session.id)
        assert len(mem1.suggested_actions) >= 2, "SessionWorkingMemory must store suggested actions in Redis"
        print("[PASS] Turn 1 Verified: Deposit account and loan discovery actions proposed with embedded markdown.\n")

        # -------------------------------------------------------------------------
        # TURN 2: Select Action 1 (View Deposit Accounts)
        # -------------------------------------------------------------------------
        print("--- [TURN 2] Triggering Action 1 (View Deposit Accounts) ---")
        account_action = next(a for a in actions1 if "account" in a.get("label", "").lower())
        q2 = account_action.get("suggested_prompt")
        print(f"Selected Prompt from Next Best Actions: {q2}")

        ChatRepository.add_message(db, session_id=session.id, role="user", content=q2)
        ans2, src2, grounded2, conf2, actions2 = orchestrator.execute_unified_query(
            query=q2,
            user_context=user_context,
            db=db,
            session_id=session.id,
            return_actions=True
        )
        ChatRepository.add_message(db, session_id=session.id, role="assistant", content=ans2)

        print(f"AI Response:\n{ans2[:250]}...\n")
        print(f"Suggested Actions Turn 2 ({len(actions2)} actions):")
        for a in actions2:
            print(f"  - [{a.get('action_type')}] {a.get('label')}: \"{a.get('suggested_prompt')}\" (conversational: \"{a.get('conversational_prompt')}\")")

        # Assertions for Turn 2:
        assert len(actions2) >= 2, "Turn 2 must propose at least 2 next best actions"
        labels2 = [a.get("label", "").lower() for a in actions2]
        assert any("transaction" in lbl or "velocity" in lbl for lbl in labels2), "Turn 2 must propose analyzing transaction activity"
        assert any("loan" in lbl for lbl in labels2), "Turn 2 must propose checking active loans"
        assert all(a.get("conversational_prompt") for a in actions2), "All actions must have conversational_prompt"
        assert all(a.get("conversational_prompt", "").endswith("?") for a in actions2), "Conversational prompts must end with '?'"
        assert "**Next steps you might consider:**" in ans2, "Turn 2 response must embed consultative recommendations block"
        print("[PASS] Turn 2 Verified: Transaction drilldown and credit check actions proposed with embedded markdown.\n")

        # -------------------------------------------------------------------------
        # TURN 3: Select Action (Analyze Transaction Velocity)
        # -------------------------------------------------------------------------
        print("--- [TURN 3] Triggering Action (Analyze Transaction Velocity) ---")
        tx_action = next(a for a in actions2 if "transaction" in a.get("label", "").lower() or "velocity" in a.get("label", "").lower())
        q3 = tx_action.get("suggested_prompt")
        print(f"Selected Prompt from Next Best Actions: {q3}")

        ChatRepository.add_message(db, session_id=session.id, role="user", content=q3)
        ans3, src3, grounded3, conf3, actions3 = orchestrator.execute_unified_query(
            query=q3,
            user_context=user_context,
            db=db,
            session_id=session.id,
            return_actions=True
        )
        ChatRepository.add_message(db, session_id=session.id, role="assistant", content=ans3)

        print(f"AI Response:\n{ans3[:250]}...\n")
        print(f"Suggested Actions Turn 3 ({len(actions3)} actions):")
        for a in actions3:
            print(f"  - [{a.get('action_type')}] {a.get('label')}: \"{a.get('suggested_prompt')}\" (conversational: \"{a.get('conversational_prompt')}\")")

        # Assertions for Turn 3:
        assert len(actions3) >= 2, "Turn 3 must propose at least 2 next best actions"
        labels3 = [a.get("label", "").lower() for a in actions3]
        assert any("loan" in lbl for lbl in labels3), "Turn 3 must propose checking loans after transaction drilldown"
        assert all(a.get("conversational_prompt") for a in actions3), "All actions must have conversational_prompt"
        assert all(a.get("conversational_prompt", "").endswith("?") for a in actions3), "Conversational prompts must end with '?'"
        assert "**Next steps you might consider:**" in ans3, "Turn 3 response must embed consultative recommendations block"
        print("[PASS] Turn 3 Verified: Credit cross-system check actions proposed with embedded markdown.\n")

        # -------------------------------------------------------------------------
        # TURN 4: Trigger Cross-Database Pivot (Check Active Loans in MSSQL)
        # -------------------------------------------------------------------------
        print("--- [TURN 4] Triggering Action (Check Active Loans in MSSQL) ---")
        loan_action = next(a for a in actions3 if "loan" in a.get("label", "").lower())
        q4 = loan_action.get("suggested_prompt")
        print(f"Selected Prompt from Next Best Actions: {q4}")

        ChatRepository.add_message(db, session_id=session.id, role="user", content=q4)
        ans4, src4, grounded4, conf4, actions4 = orchestrator.execute_unified_query(
            query=q4,
            user_context=user_context,
            db=db,
            session_id=session.id,
            return_actions=True
        )
        ChatRepository.add_message(db, session_id=session.id, role="assistant", content=ans4)

        print(f"AI Response:\n{ans4[:250]}...\n")
        print(f"Suggested Actions Turn 4 ({len(actions4)} actions):")
        for a in actions4:
            print(f"  - [{a.get('action_type')}] {a.get('label')}: \"{a.get('suggested_prompt')}\" (conversational: \"{a.get('conversational_prompt')}\")")

        # Assertions for Turn 4:
        assert len(actions4) >= 2, "Turn 4 must propose at least 2 next best actions"
        labels4 = [a.get("label", "").lower() for a in actions4]
        assert any("policy" in lbl or "guidelines" in lbl for lbl in labels4), "Turn 4 must propose enterprise lending policy review"
        assert any("compliance" in lbl or "audit" in lbl for lbl in labels4), "Turn 4 must propose compliance audit"
        assert all(a.get("conversational_prompt") for a in actions4), "All actions must have conversational_prompt"
        assert all(a.get("conversational_prompt", "").endswith("?") for a in actions4), "Conversational prompts must end with '?'"
        assert "**Next steps you might consider:**" in ans4, "Turn 4 response must embed consultative recommendations block"
        print("[PASS] Turn 4 Verified: Enterprise policy guidelines and compliance actions proposed with embedded markdown.\n")

        # -------------------------------------------------------------------------
        # TEST SCHEMA / METADATA QUERY PROTECTION (Screenshot Bug Fix)
        # -------------------------------------------------------------------------
        print("--- [TEST] Verifying Schema Query Protection Against False Collection Traps ---")
        q_schema = "What customer profile and KYC attributes are stored in our customer database?"
        mem_schema = SessionWorkingMemory(session_id="test_schema", scope=EntityScope.COLLECTION.value)
        schema_actions = ActionProposer.propose_actions(
            memory=mem_schema,
            query=q_schema,
            answer="The database stores first_name, last_name, bvn, email, phone, and employer.",
            strategy_used="sql"
        )
        print(f"Schema Query Actions ({len(schema_actions)}):")
        for a in schema_actions:
            print(f"  - {a.label}: \"{a.conversational_prompt}\" (prompt: \"{a.suggested_prompt}\")")
            assert "branch" not in a.label.lower(), "Schema query must not falsely propose branch addresses"
            assert "branch" not in a.suggested_prompt.lower(), "Schema query must not falsely propose branch addresses"
        assert any("bvn" in a.label.lower() or "customer" in a.label.lower() for a in schema_actions), "Must propose customer lookup by BVN"
        assert any("schema" in a.label.lower() or "banking" in a.label.lower() for a in schema_actions), "Must propose core banking schema inspection"
        assert all(a.conversational_prompt and a.conversational_prompt.endswith("?") for a in schema_actions), "Schema actions must be consultative questions"

        embedded_schema_block = ActionProposer.format_embedded_recommendations(schema_actions)
        print(f"Embedded Recommendations Block for Schema Query:\n{embedded_schema_block}\n")
        assert "**Next steps you might consider:**" in embedded_schema_block
        assert "Would you like to look up an individual customer profile" in embedded_schema_block
        print("[PASS] Schema query protection verified with zero false collection traps.\n")

        # -------------------------------------------------------------------------
        # TEST SCOPE ISOLATION: SYSTEM_META & AGGREGATE SCOPES
        # -------------------------------------------------------------------------
        print("--- [TEST] Verifying Scope Isolation & Zero Context Bleed in Actions ---")
        
        # 1. SYSTEM_META scope: Must NEVER propose personal banking or single-customer actions
        mem_meta = SessionWorkingMemory(session_id="test_meta", scope=EntityScope.SYSTEM_META.value)
        meta_actions = ActionProposer.propose_actions(
            memory=mem_meta,
            query="How many data sources are registered on this system?",
            answer="There are 7 active data sources registered.",
            strategy_used="system_meta"
        )
        print(f"System Meta Actions ({len(meta_actions)}):")
        for a in meta_actions:
            print(f"  - {a.label}: \"{a.suggested_prompt}\" (conversational: \"{a.conversational_prompt}\")")
            assert "adebayo" not in a.suggested_prompt.lower(), "System meta action must not leak customer name"
            assert "1008" not in a.suggested_prompt, "System meta action must not leak customer ID"
            assert a.conversational_prompt and a.conversational_prompt.endswith("?"), "Must be phrased as a consultative question"
        assert len(meta_actions) >= 2, "System meta must propose at least 2 actions"
        print("[PASS] SYSTEM_META scope actions isolated with zero entity leakage.")

        # 2. AGGREGATE scope: Must propose macro-level actions without single-customer filters
        mem_agg = SessionWorkingMemory(session_id="test_agg", scope=EntityScope.AGGREGATE.value)
        agg_actions = ActionProposer.propose_actions(
            memory=mem_agg,
            query="Apart from Dangote, which other industries do we have customers from?",
            answer="Customers belong to Construction, Consumer Goods, and Financial Services.",
            strategy_used="sql"
        )
        print(f"Aggregate Scope Actions ({len(agg_actions)}):")
        for a in agg_actions:
            print(f"  - {a.label}: \"{a.suggested_prompt}\" (conversational: \"{a.conversational_prompt}\")")
            assert "adebayo" not in a.suggested_prompt.lower(), "Aggregate action must not leak customer name"
            assert "1008" not in a.suggested_prompt, "Aggregate action must not leak customer ID"
            assert a.conversational_prompt and a.conversational_prompt.endswith("?"), "Must be phrased as a consultative question"
        assert len(agg_actions) >= 2, "Aggregate scope must propose at least 2 macro actions"
        print("[PASS] AGGREGATE scope actions isolated with zero entity leakage.")

        print("=" * 80)
        print("ALL PROACTIVE AGENT COGNITION & NEXT BEST ACTION BENCHMARKS PASSED 100%!")
        print("=" * 80)

    finally:
        db.close()

if __name__ == "__main__":
    run_action_proposer_benchmark()
