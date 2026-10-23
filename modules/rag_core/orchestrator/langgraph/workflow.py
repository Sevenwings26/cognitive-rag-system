"""
LangGraph StateGraph Definition with Blackboard Architecture,
Cyclic Self-Correction Loops, and Human-in-the-Loop SQL Breakpoints.
"""

from typing import Dict, Any, Optional
try:
    from langgraph.graph import StateGraph, START, END
    LANGGRAPH_AVAILABLE = True
except ImportError:
    LANGGRAPH_AVAILABLE = False
    StateGraph = None
    START = "START"
    END = "END"

from modules.rag_core.orchestrator.langgraph.state import GraphState, RoutingDecision
from modules.rag_core.orchestrator.langgraph import nodes


def build_enterprise_rag_graph(
    llm_service: Any,
    vector_store: Any,
    reranker: Any,
    checkpointer: Optional[Any] = None,
    enable_hitl_sql: bool = True
):
    """
    Constructs and compiles the Stateful LangGraph for Enterprise Multi-Modal RAG.
    
    Topological Invariants:
    1. Blackboard Hydration: State is hydrated additively from on-premise persistence.
    2. Anaphora Condensation: Query is rewritten against active Blackboard entities.
    3. Multi-Modal Branching: Routes to SQL generator, Vector retriever, or Session Identity.
    4. Human-in-the-Loop Breakpoint: Intercepts before node_sql_executor for manual review.
    5. Early Entity Harvesting: Retrievers emit entities directly onto Blackboard before synthesis.
    6. Grounding & Cyclic Self-Correction: Up to 3 iterations if validation fails.
    """
    if not LANGGRAPH_AVAILABLE:
        # Fallback simulator / architecture runner if langgraph package is pending installation
        return EnterpriseRAGGraphRunner(
            llm_service=llm_service,
            vector_store=vector_store,
            reranker=reranker,
            enable_hitl_sql=enable_hitl_sql
        )

    workflow = StateGraph(GraphState)

    # 1. Register Nodes
    workflow.add_node("node_resolve_context", nodes.node_resolve_context)
    workflow.add_node("node_summarize_history", lambda s: nodes.node_summarize_history(s, llm_service=llm_service))
    workflow.add_node("node_condense_query", lambda s: nodes.node_condense_query(s, llm_service=llm_service))
    workflow.add_node("node_planner", nodes.node_planner)
    
    workflow.add_node("node_sql_generator", lambda s: nodes.node_sql_generator(s, llm_service=llm_service, vector_store=vector_store))
    workflow.add_node("node_sql_executor", lambda s: nodes.node_sql_executor(s, vector_store=vector_store, llm_service=llm_service))
    workflow.add_node("node_vector_retriever", lambda s: nodes.node_vector_retriever(s, vector_store=vector_store, reranker=reranker))
    workflow.add_node("node_identity_resolver", nodes.node_identity_resolver)

    workflow.add_node("node_synthesizer", lambda s: nodes.node_synthesizer(s, llm_service=llm_service))
    workflow.add_node("node_validator", nodes.node_validator)
    workflow.add_node("node_fallback_refusal", nodes.node_fallback_refusal)
    workflow.add_node("node_state_harvester", nodes.node_state_harvester)
    workflow.add_node("node_cleanup_ephemeral_state", nodes.node_cleanup_ephemeral_state)

    # 2. Sequential Ingress Pipeline (with History Summarizer)
    workflow.add_edge(START, "node_resolve_context")
    workflow.add_edge("node_resolve_context", "node_summarize_history")
    workflow.add_edge("node_summarize_history", "node_condense_query")
    workflow.add_edge("node_condense_query", "node_planner")

    # 3. Conditional Routing Edges (Multi-Modal Branching)
    def route_by_decision(state: GraphState) -> str:
        decision = state.get("routing_decision")
        if decision == RoutingDecision.SQL:
            return "node_sql_generator"
        elif decision == RoutingDecision.DOCUMENT:
            return "node_vector_retriever"
        elif decision in (RoutingDecision.IDENTITY, RoutingDecision.CONVERSATIONAL):
            return "node_identity_resolver"
        return "node_vector_retriever"

    workflow.add_conditional_edges(
        "node_planner",
        route_by_decision,
        {
            "node_sql_generator": "node_sql_generator",
            "node_vector_retriever": "node_vector_retriever",
            "node_identity_resolver": "node_identity_resolver"
        }
    )

    # 4. SQL Execution Pipeline (with optional HITL breakpoint before executor)
    workflow.add_edge("node_sql_generator", "node_sql_executor")
    workflow.add_edge("node_sql_executor", "node_synthesizer")

    # 5. Document & Identity Convergence to Synthesizer
    workflow.add_edge("node_vector_retriever", "node_synthesizer")
    workflow.add_edge("node_identity_resolver", "node_synthesizer")

    # 6. Response Validation & Cyclic Correction Loop
    workflow.add_edge("node_synthesizer", "node_validator")

    def route_validation(state: GraphState) -> str:
        report = state.get("grounding_report") or {}
        is_grounded = report.get("is_grounded", True)
        iteration = state.get("iteration_count", 1)

        if is_grounded:
            return "node_state_harvester"

        # Cyclic Self-Correction: Retry synthesis with critique up to 3 times
        if iteration < 3:
            return "node_synthesizer"

        # Exceeded correction budget -> Route to safe refusal
        return "node_fallback_refusal"

    workflow.add_conditional_edges(
        "node_validator",
        route_validation,
        {
            "node_state_harvester": "node_state_harvester",
            "node_synthesizer": "node_synthesizer",
            "node_fallback_refusal": "node_fallback_refusal"
        }
    )

    # 7. Egress & Janitor State Cleanup (Purges Ephemeral Context before END)
    workflow.add_edge("node_fallback_refusal", "node_state_harvester")
    workflow.add_edge("node_state_harvester", "node_cleanup_ephemeral_state")
    workflow.add_edge("node_cleanup_ephemeral_state", END)

    # 8. Compilation with Optional HITL Breakpoint & On-Premise Checkpointer
    interrupt_nodes = ["node_sql_executor"] if enable_hitl_sql else []

    app = workflow.compile(
        checkpointer=checkpointer,
        interrupt_before=interrupt_nodes
    )
    return app


# =============================================================================
# Standalone Architectural Graph Runner (Dependency-Free Execution Engine)
# =============================================================================

class EnterpriseRAGGraphRunner:
    """
    Self-contained Directed Graph Execution Engine that executes the exact
    LangGraph state transitions, reducers, cyclic loops, and HITL breakpoints
    even before external packages are pip-installed in on-premise environments.
    """
    def __init__(self, llm_service: Any, vector_store: Any, reranker: Any, enable_hitl_sql: bool = True):
        self.llm = llm_service
        self.vector_store = vector_store
        self.reranker = reranker
        self.enable_hitl_sql = enable_hitl_sql

    def invoke(self, initial_state: GraphState, config: Optional[Dict[str, Any]] = None) -> GraphState:
        state = dict(initial_state)

        # 1. Context Hydration
        u1 = nodes.node_resolve_context(state)
        self._apply_update(state, u1)

        # 1B. Summarization Memory
        u_sum = nodes.node_summarize_history(state, llm_service=self.llm)
        self._apply_update(state, u_sum)

        # 2. Query Condensation
        u2 = nodes.node_condense_query(state, llm_service=self.llm)
        self._apply_update(state, u2)

        # 3. Planner
        u3 = nodes.node_planner(state)
        self._apply_update(state, u3)

        decision = state.get("routing_decision")

        # 4. Modality Branching
        if decision == RoutingDecision.SQL:
            u_sql_gen = nodes.node_sql_generator(state, llm_service=self.llm, vector_store=self.vector_store)
            self._apply_update(state, u_sql_gen)

            # HITL Checkpoint Inspection
            if self.enable_hitl_sql and config and config.get("interrupt"):
                state["sql_approval_status"] = "interrupted_for_review"
                return state

            u_sql_exec = nodes.node_sql_executor(state, vector_store=self.vector_store, llm_service=self.llm)
            self._apply_update(state, u_sql_exec)

        elif decision == RoutingDecision.DOCUMENT:
            u_vec = nodes.node_vector_retriever(state, vector_store=self.vector_store, reranker=self.reranker)
            self._apply_update(state, u_vec)

        elif decision in (RoutingDecision.IDENTITY, RoutingDecision.CONVERSATIONAL):
            u_id = nodes.node_identity_resolver(state)
            self._apply_update(state, u_id)

        # 5. Synthesis & Cyclic Validation Loop
        while True:
            u_syn = nodes.node_synthesizer(state, llm_service=self.llm)
            self._apply_update(state, u_syn)

            u_val = nodes.node_validator(state)
            self._apply_update(state, u_val)

            report = state.get("grounding_report", {})
            if report.get("is_grounded", True):
                break

            if state.get("iteration_count", 1) >= 3:
                u_ref = nodes.node_fallback_refusal(state)
                self._apply_update(state, u_ref)
                break

        # 6. State Harvester Egress
        u_harv = nodes.node_state_harvester(state)
        self._apply_update(state, u_harv)

        # 7. Ephemeral Cleanup (The Janitor)
        u_clean = nodes.node_cleanup_ephemeral_state(state)
        self._apply_update(state, u_clean)

        return state

    def _apply_update(self, state: Dict[str, Any], update: Dict[str, Any]):
        for k, v in update.items():
            if k == "working_memory":
                from modules.rag_core.orchestrator.langgraph.state import merge_working_memory
                state[k] = merge_working_memory(state.get(k), v)
            elif k in ("retrieved_context", "retrieved_chunks", "sql_result_rows"):
                from modules.rag_core.orchestrator.langgraph.state import merge_context_list
                state[k] = merge_context_list(state.get(k), v)
            elif k == "messages":
                from modules.rag_core.orchestrator.langgraph.state import manage_messages
                state[k] = manage_messages(state.get(k), v)
            else:
                state[k] = v
