# benchmarking/test_20_turn_manual_audit.py
import sys
import os
import uuid
import json
import logging
from typing import List, Dict, Any

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if BASE_DIR not in sys.path:
    sys.path.insert(0, BASE_DIR)

from core.database import SessionLocal
from modules.auth.domain.models import User, Organization
from modules.auth.domain.tokens import TokenData
from modules.rag_core.orchestrator.unified_orchestrator import UnifiedRAGOrchestrator
from modules.rag_core.context import SessionContextManager, SessionWorkingMemory
from modules.governance.repositories.chat_repository import ChatRepository

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("conversational_audit")

TEST_TURNS = [
    (1, "What is the primary contact of the MTN vendor?"),
    (2, "Okay. I need to look into the bank system, confirm Who is the customer associated with Bank Verification Number (BVN) 90000001008?"),
    (3, "Which customers have generated more than 10 million in transaction volume in the last 12 months and currently maintain active loan facilities?"),
    (4, "About this: Which customers have generated more than 10 million in transaction volume in the last 12 months and currently maintain active loan facilities? Check the database source, if it will found"),
    (5, "back to that vendor, what is his email address?"),
    (6, "I asked for the MTN vendor name in the first chat"),
    (7, "What is the primary contact of the MTN vendor?"),
    (8, "What is his email address to contact"),
    (9, "which vendor offers Samsung sales?"),
    (10, "What if you cite the sharepoint source for the Samsung"),
    (11, "Alright"),
    (12, "okay"),
    (13, "How many data sources do we have on this system"),
    (14, "Who is the customer associated with Bank Verification Number (BVN) 90000001008?"),
    (15, "What bank accounts does he have with us, and what is his current total deposit balance?"),
    (16, "Which other customer employers like dangote do we have in our bank, if we can target them for loan offers"),
    (17, "Nestle Nigeria PLC pays well, can you fetch their employee who are our customers?"),
    (18, "Okay what about customers that have Nestle recorded as their employer?"),
    (19, "What are their account balances?"),
    (20, "How many data sources are we connected to, and list them... If any failure occur, identify the pipeline that caused."),
]

def run_20_turn_conversational_audit():
    db = SessionLocal()
    orchestrator = UnifiedRAGOrchestrator()

    try:
        print("\n" + "=" * 80)
        print("STARTING 20-TURN REAL-WORLD CONVERSATIONAL AUDIT BENCHMARK")
        print("=" * 80)

        # 1. Resolve Super Admin User & Organization Context
        user = db.query(User).filter(User.email == "iarowosola@yahoo.com").first()
        if not user:
            user = db.query(User).first()
        assert user is not None, "Test user must exist in database"

        org = db.query(Organization).filter(Organization.id == user.org_id).first()
        assert org is not None, "Organization must exist in database"

        user_context = TokenData(
            user_id=user.id,
            email=user.email,
            org_id=org.id,
            role=user.role,
            department_id=user.department_id
        )

        session_id = f"audit-20turn-{uuid.uuid4().hex[:8]}"
        session = ChatRepository.get_or_create_session(
            db=db,
            session_id=session_id,
            org_id=user_context.org_id,
            user_id=user_context.user_id,
            title="20-Turn Real World Audit"
        )

        print(f"Active Diagnostic Session ID: {session.id}")
        print(f"Tenant: {org.name} | User: {user.email}\n")

        execution_traces: List[Dict[str, Any]] = []

        for turn_num, query in TEST_TURNS:
            print(f"\n{'='*40} TURN {turn_num} {'='*40}")
            print(f"[USER PROMPT]: {query}")

            try:
                ChatRepository.add_message(db, session_id=session_id, role="user", content=query)
                db.commit()

                ans, sources, is_grounded, confidence = orchestrator.execute_unified_query(
                    query=query,
                    user_context=user_context,
                    db=db,
                    session_id=session_id
                )

                ChatRepository.add_message(
                    db=db,
                    session_id=session_id,
                    role="assistant",
                    content=ans,
                    citation_metadata={
                        "sources": sources,
                        "is_grounded": is_grounded,
                        "confidence": confidence
                    }
                )
                db.commit()
            except Exception as turn_err:
                logger.error(f"[ERROR IN TURN {turn_num}]: {turn_err}", exc_info=True)
                try:
                    db.rollback()
                except Exception:
                    pass
                ans = f"Error during execution: {turn_err}"
                sources = []
                is_grounded = False
                confidence = 0.0

            mem: SessionWorkingMemory = SessionContextManager.get_memory(session_id)

            target_db = mem.last_target_database
            generated_sql = ""
            for s in sources:
                if s.get("sql_query"):
                    generated_sql = s.get("sql_query")
                if s.get("database_name"):
                    target_db = s.get("database_name")

            strategy_used = mem.last_strategy or "unknown"
            if not sources and ("conversational" in strategy_used or turn_num in (11, 12)):
                strategy_used = "conversational"

            trace_item = {
                "turn": turn_num,
                "query": query,
                "strategy": strategy_used,
                "target_db": target_db or "N/A",
                "generated_sql": generated_sql or "N/A",
                "blackboard_state": {
                    "scope": mem.scope,
                    "active_entities": mem.active_entities,
                    "active_names": mem.active_names,
                    "active_vendors": getattr(mem, "active_vendors", []),
                    "collection_ids": getattr(mem, "collection_ids", []),
                    "active_documents": mem.active_documents,
                    "active_metrics": mem.active_metrics
                },
                "sources": [s.get("source_name") or s.get("filename") for s in sources],
                "answer": ans
            }
            execution_traces.append(trace_item)

            print(f"[STRATEGY]: {strategy_used} | [TARGET DB]: {target_db}")
            if generated_sql:
                print(f"[SQL]: {generated_sql}")
            print(f"[BLACKBOARD]: Scope={mem.scope} | Entities={mem.active_entities} | Names={mem.active_names} | CollectionIDs={getattr(mem, 'collection_ids', [])} | Vendors={getattr(mem, 'active_vendors', [])}")
            print(f"[SOURCES]: {trace_item['sources']}")
            print(f"[AI RESPONSE]:\n{ans}\n")

        # Save trace JSON
        trace_path = os.path.join(BASE_DIR, "benchmarking", "20_turn_audit_trace.json")
        with open(trace_path, "w", encoding="utf-8") as f:
            json.dump(execution_traces, f, indent=2)

        print("\n" + "=" * 80)
        print(f"20-TURN AUDIT FINISHED. TRACE SAVED TO: {trace_path}")
        print("=" * 80)

    finally:
        db.close()

if __name__ == "__main__":
    run_20_turn_conversational_audit()
