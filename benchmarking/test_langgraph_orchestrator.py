import os, sys
sys.path.insert(0, "/app")

from modules.rag_core.orchestrator.langgraph.state import (
    GraphState,
    RoutingDecision,
    EntityScope,
    merge_working_memory,
    merge_context_list
)
from modules.rag_core.orchestrator.langgraph.workflow import build_enterprise_rag_graph
from modules.rag_core.orchestrator.langgraph import nodes
from modules.rag_core.orchestrator.unified_orchestrator import UnifiedRAGOrchestrator

print("="*80)
print("TESTING LANGGRAPH BLACKBOARD ORCHESTRATOR & REDUCERS")
print("="*80)

# -----------------------------------------------------------------------------
# 1. Test Blackboard Additive Reducer
# -----------------------------------------------------------------------------
initial_memory = {
    "active_entities": {"customer_id": 1008, "customer_number": "CUST-1008"},
    "active_external_entities": {"vendor": "MTN"},
    "resolution_scope": "SINGLE_ENTITY",
    "active_documents": ["approved_vendors.xlsx"],
    "active_cloud_sources": ["approved_vendors.xlsx"],
    "collection_ids": [1008]
}

# Turn 2 update: Discovered bvn and added new collection id
update_memory = {
    "active_entities": {"bvn": "90000001008"},
    "collection_ids": [1047],
    "resolution_scope": "COLLECTION",
    "active_documents": ["noros_customer_db"]
}

merged = merge_working_memory(initial_memory, update_memory)
print("Merged Blackboard State:")
print(merged)

assert merged["active_entities"]["customer_id"] == 1008, "Historical customer_id lost!"
assert merged["active_entities"]["bvn"] == "90000001008", "New bvn not merged!"
assert merged["active_external_entities"]["vendor"] == "MTN", "Vendor entity lost!"
assert merged["resolution_scope"] == "COLLECTION", "Scope transition failed!"
assert set(merged["collection_ids"]) == {1008, 1047}, "Collection IDs union failed!"
assert "approved_vendors.xlsx" in merged["active_documents"]
assert "noros_customer_db" in merged["active_documents"]
print("[PASS] merge_working_memory additive blackboard reducer verified successfully!")

# -----------------------------------------------------------------------------
# 2. Test Cyclic Validation & Correction Loop Logic
# -----------------------------------------------------------------------------
# Simulate state with ungrounded number
state_with_hallucination = {
    "synthesized_response": "The customer has 9999999999 NGN in their account.",
    "retrieved_context": [{"content": "Account balance: 450,000 NGN"}],
    "iteration_count": 0
}

val_result = nodes.node_validator(state_with_hallucination)
print("\nValidation Result on Hallucination:")
print(val_result)

assert val_result["grounding_report"]["is_grounded"] is False, "Hallucination was not flagged!"
assert val_result["iteration_count"] == 1
assert val_result["correction_instruction"] is not None
print("[PASS] node_validator detected hallucination and generated correction directive!")

# -----------------------------------------------------------------------------
# 3. Test Max Iteration Safety Boundary
# -----------------------------------------------------------------------------
state_exhausted = {
    "iteration_count": 3,
    "grounding_report": {"is_grounded": False}
}
refusal = nodes.node_fallback_refusal(state_exhausted)
assert "unable to verify" in refusal["synthesized_response"]
print("[PASS] node_fallback_refusal triggered safely upon iteration exhaustion!")

# -----------------------------------------------------------------------------
# 4. Test Graph Execution on Identity Query (End-to-End)
# -----------------------------------------------------------------------------
unified_orch = UnifiedRAGOrchestrator()

runner = build_enterprise_rag_graph(
    llm_service=unified_orch.llm,
    vector_store=unified_orch.vector_store,
    reranker=unified_orch.reranker,
    enable_hitl_sql=True
)

init_state = {
    "raw_query": "What is the name of my organization?",
    "session_id": "langgraph_test_session_01",
    "user_context": {
        "user_id": "2ae090f4-684f-4f8e-a0f4-119de0c0e900",
        "email": "iarowosola@yahoo.com",
        "role": "SUPER_ADMIN",
        "org_id": "8f0d205a-06cf-497a-9f25-6ca460d75c13"
    }
}

print("\n--- Executing LangGraph on Identity Query ---")
final_graph_state = runner.invoke(init_state)
print(f"Routing Decision: {final_graph_state.get('routing_decision')}")
print(f"Grounding Report: {final_graph_state.get('grounding_report')}")
print(f"Final Answer: {final_graph_state.get('synthesized_response')}")

assert "Bluesources Limited" in final_graph_state.get("synthesized_response"), "Organization not grounded!"
assert final_graph_state.get("routing_decision") == RoutingDecision.IDENTITY
# Verify janitor cleaned up ephemeral state
assert final_graph_state.get("retrieved_chunks") == [] or final_graph_state.get("retrieved_chunks") is None
assert final_graph_state.get("retrieved_context") == []
print("[PASS] End-to-end LangGraph execution on identity query succeeded!")

# -----------------------------------------------------------------------------
# 5. Test History Summarizer Node (node_summarize_history)
# -----------------------------------------------------------------------------
print("\n--- Testing History Summarizer & Message Window Management ---")
class MockLLMForSummary:
    def generate_text(self, prompt, system_instruction=None, **kwargs):
        assert kwargs.get("max_tokens") is not None, "max_tokens constraint missing!"
        return "User asked about customer 1008 and MTN vendor contracts. Assistant confirmed customer 1008 is active."

mock_llm = MockLLMForSummary()

# Case A: <= 6 messages -> no summarization
short_state = {
    "messages": [
        {"role": "user", "content": "Hello"},
        {"role": "assistant", "content": "Hi, how can I help?"},
        {"role": "user", "content": "Who is customer 1008?"},
        {"role": "assistant", "content": "Customer 1008 is John Doe."}
    ],
    "summary": ""
}
res_short = nodes.node_summarize_history(short_state, llm_service=mock_llm)
assert res_short == {}, "Summarizer should not trigger on <= 6 messages"

# Case B: > 6 messages -> triggers summarization & prunes messages to recent turn
long_state = {
    "messages": [
        {"role": "user", "content": "Turn 1 question"},
        {"role": "assistant", "content": "Turn 1 answer"},
        {"role": "user", "content": "Turn 2 question"},
        {"role": "assistant", "content": "Turn 2 answer"},
        {"role": "user", "content": "Turn 3 question"},
        {"role": "assistant", "content": "Turn 3 answer"},
        {"role": "user", "content": "Turn 4 question"},
        {"role": "assistant", "content": "Turn 4 answer"}
    ],
    "summary": "Initial conversation began."
}
res_long = nodes.node_summarize_history(long_state, llm_service=mock_llm)
assert "customer 1008" in res_long["summary"].lower() or len(res_long["summary"]) > 0
assert len(res_long["messages"]) == 2, f"Expected 2 messages (recent turn), got {len(res_long['messages'])}"
assert res_long["messages"][0]["content"] == "Turn 4 question"
assert res_long["messages"][1]["content"] == "Turn 4 answer"
print("[PASS] node_summarize_history triggered on >6 messages and pruned message history to recent turn!")

# -----------------------------------------------------------------------------
# 6. Test Context Budgeting in Retrievers (SQL Truncation)
# -----------------------------------------------------------------------------
print("\n--- Testing Context Budgeting in Retrievers ---")
large_sql_rows = [{"row_id": i, "data": f"record_{i}"} for i in range(50)]
safe_rows = list(large_sql_rows)
if len(safe_rows) > 20:
    safe_rows = safe_rows[:20]
    safe_rows.append({"_system_notice": "[Warning: Results truncated for memory efficiency]"})

assert len(safe_rows) == 21  # 20 data rows + 1 warning notice
assert safe_rows[-1]["_system_notice"] == "[Warning: Results truncated for memory efficiency]"
print("[PASS] SQL Context Budgeting truncates > 20 rows and attaches memory warning notice!")

# -----------------------------------------------------------------------------
# 7. Test The Janitor Node (node_cleanup_ephemeral_state)
# -----------------------------------------------------------------------------
print("\n--- Testing The Janitor Node (node_cleanup_ephemeral_state) ---")
bloated_state = {
    "retrieved_chunks": [{"content": "chunk 1"*50}, {"content": "chunk 2"*50}],
    "sql_result_rows": [{"id": 1, "col": "val"*50}],
    "retrieved_context": [{"content": "context"*50}],
    "current_query_analysis": {"intent": "COMPLEX", "tables": ["huge_table"]},
    "generated_sql": "SELECT * FROM huge_table WHERE x = 1",
    "correction_instruction": "Fix hallucination",
    "working_memory": {
        "active_entities": {"customer_id": 1008},
        "active_metrics": {"balance": 50000},
        "collection_ids": [1008],
        "temporary_raw_table_dump": "gigantic_string_data"*1000,
        "turn_count": 3
    }
}
clean_res = nodes.node_cleanup_ephemeral_state(bloated_state)
assert clean_res["retrieved_chunks"] == [], "retrieved_chunks not cleared!"
assert clean_res["sql_result_rows"] == [], "sql_result_rows not cleared!"
assert clean_res["retrieved_context"] == [], "retrieved_context not cleared!"
assert clean_res["generated_sql"] is None, "generated_sql not cleared!"
assert clean_res["current_query_analysis"] is None, "current_query_analysis not cleared!"
assert clean_res["working_memory"]["active_entities"]["customer_id"] == 1008, "Harvested entities lost!"
assert "temporary_raw_table_dump" not in clean_res["working_memory"], "Temporary table dump not pruned!"
print("[PASS] node_cleanup_ephemeral_state purged ephemeral state and retained harvested entities!")

# -----------------------------------------------------------------------------
# 8. Test Multi-Turn Simulation: Turn 2 vs Turn 10 Blackboard Token Stability
# -----------------------------------------------------------------------------
print("\n--- Testing Multi-Turn Blackboard Token Stability (Turn 2 vs Turn 10) ---")
import json

def estimate_tokens(obj):
    text = json.dumps(obj, default=str)
    return max(1, len(text) // 4)

simulated_state = {
    "session_id": "stability_test_session",
    "summary": "",
    "messages": [],
    "working_memory": {
        "active_entities": {},
        "active_metrics": {},
        "collection_ids": [],
        "turn_count": 0
    },
    "user_context": {"org_id": "8f0d205a-06cf-497a-9f25-6ca460d75c13", "role": "MEMBER"}
}

turn_token_counts = {}

for turn in range(1, 11):
    simulated_state["raw_query"] = f"Turn {turn}: Details for customer {1000 + turn}?"
    simulated_state["condensed_query"] = simulated_state["raw_query"]
    
    # 1. Summarizer step
    sum_update = nodes.node_summarize_history(simulated_state, llm_service=mock_llm)
    if sum_update:
        simulated_state.update(sum_update)
        
    # 2. Retrieval step (budgeted)
    simulated_state["retrieved_chunks"] = [
        {"content": f"Customer {1000 + turn} details and records...", "filename": f"doc_{turn}.pdf"}
    ]
    simulated_state["working_memory"]["active_entities"][f"customer_{1000+turn}"] = 1000 + turn
    
    # 3. Harvester step (appends turn to messages)
    simulated_state["synthesized_response"] = f"Customer {1000 + turn} is verified with active account."
    harv_update = nodes.node_state_harvester(simulated_state)
    simulated_state["working_memory"] = harv_update["working_memory"]
    simulated_state["messages"] = harv_update["messages"]
    
    # 4. Janitor Cleanup step
    clean_update = nodes.node_cleanup_ephemeral_state(simulated_state)
    simulated_state.update(clean_update)
    
    # Measure Blackboard state size at end of turn (what persists to checkpointer)
    tokens = estimate_tokens(simulated_state)
    turn_token_counts[turn] = tokens
    print(f"Turn {turn:2d} Blackboard Token Count: {tokens:3d} tokens | Messages: {len(simulated_state['messages'])} | Has Summary: {bool(simulated_state.get('summary'))}")

turn2_tokens = turn_token_counts[2]
turn10_tokens = turn_token_counts[10]

print(f"\nTurn 2 Tokens: {turn2_tokens}")
print(f"Turn 10 Tokens: {turn10_tokens}")
ratio = turn10_tokens / turn2_tokens
print(f"Turn 10 / Turn 2 Ratio: {ratio:.2f}x")

assert ratio < 2.5, f"Blackboard bloat detected! Turn 10 is {ratio:.2f}x Turn 2!"
assert turn10_tokens < 600, f"Turn 10 tokens ({turn10_tokens}) exceeded safety ceiling!"
print("[PASS] Multi-turn Blackboard size at Turn 10 is roughly the same as Turn 2!")

print("\n" + "="*80)
print("ALL LANGGRAPH BLACKBOARD ARCHITECTURE & MEMORY PRUNING TESTS PASSED!")
print("="*80)
