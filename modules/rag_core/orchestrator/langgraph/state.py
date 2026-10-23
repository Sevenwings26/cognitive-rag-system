"""
Blackboard State Definition for LangGraph Enterprise RAG Orchestrator.

Implements an additive, multi-turn Blackboard pattern with custom state reducers.
"""

from typing import TypedDict, Dict, Any, List, Optional, Annotated
from enum import Enum
from pydantic import BaseModel, Field


class RoutingDecision(str, Enum):
    SQL = "sql"
    DOCUMENT = "document"
    IDENTITY = "identity"
    CONVERSATIONAL = "conversational"
    SYSTEM_META = "system_meta"


class EntityScope(str, Enum):
    SINGLE_ENTITY = "SINGLE_ENTITY"
    COLLECTION = "COLLECTION"
    AGGREGATE = "AGGREGATE"
    SYSTEM_META = "SYSTEM_META"


def merge_working_memory(current: Optional[Dict[str, Any]], update: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Additive Blackboard Reducer.
    
    Merges newly discovered entities, metrics, and sources into the session blackboard
    without dropping historical context from previous turns or earlier nodes in the same turn.
    """
    if not current:
        return update or {}
    if not update:
        return current

    merged = dict(current)

    # 1. Additive Entity Merge (Preserve historical keys, update if new value provided)
    cur_entities = dict(merged.get("active_entities") or {})
    new_entities = update.get("active_entities") or {}
    cur_entities.update(new_entities)
    merged["active_entities"] = cur_entities

    # 2. Additive External Entities (Vendors, Partners)
    cur_ext = dict(merged.get("active_external_entities") or {})
    new_ext = update.get("active_external_entities") or {}
    cur_ext.update(new_ext)
    merged["active_external_entities"] = cur_ext

    # 3. Additive Metrics (Account balances, principal loans, transaction sums)
    cur_metrics = dict(merged.get("active_metrics") or {})
    new_metrics = update.get("active_metrics") or {}
    cur_metrics.update(new_metrics)
    merged["active_metrics"] = cur_metrics

    # 4. Scope and Collection Transition Rules
    if "resolution_scope" in update and update["resolution_scope"]:
        merged["resolution_scope"] = update["resolution_scope"]
    elif "scope" in update and update["scope"]:
        merged["resolution_scope"] = update["scope"]

    if "collection_ids" in update and update["collection_ids"] is not None:
        existing_cids = list(merged.get("collection_ids") or [])
        incoming_cids = list(update.get("collection_ids") or [])
        # Union while preserving order
        merged["collection_ids"] = list(dict.fromkeys(existing_cids + incoming_cids))

    # 5. Additive Sources & Documents
    cur_docs = list(merged.get("active_documents") or [])
    new_docs = update.get("active_documents") or []
    for d in new_docs:
        if d and d not in cur_docs:
            cur_docs.append(d)
    merged["active_documents"] = cur_docs

    cur_clouds = list(merged.get("active_cloud_sources") or [])
    new_clouds = update.get("active_cloud_sources") or []
    for c in new_clouds:
        if c and c not in cur_clouds:
            cur_clouds.append(c)
    merged["active_cloud_sources"] = cur_clouds

    # 6. Active Relational Tables
    cur_tables = list(merged.get("active_tables") or [])
    new_tables = update.get("active_tables") or []
    for t in new_tables:
        if t and t not in cur_tables:
            cur_tables.append(t)
    merged["active_tables"] = cur_tables

    # 7. Metadata and Anchor Tracking
    for k in ["primary_anchor_id", "last_strategy", "last_target_database"]:
        if k in update and update[k] is not None:
            merged[k] = update[k]

    # If an explicit pruned replacement is passed (from node_cleanup_ephemeral_state)
    if update.get("__pruned__") or update.get("__replace__"):
        clean = dict(update)
        clean.pop("__pruned__", None)
        clean.pop("__replace__", None)
        return clean

    return merged


def merge_context_list(
    current: Optional[List[Dict[str, Any]]], 
    update: Optional[List[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """Context list reducer: overwrites ephemeral context per turn rather than accumulating."""
    if update is None:
        return current or []
    return update


def manage_messages(
    current: Optional[List[Dict[str, Any]]],
    update: Optional[List[Dict[str, Any]]]
) -> List[Dict[str, Any]]:
    """
    Managed Messages Reducer.
    Adopts the incoming update if provided, preventing unmanaged unbounded growth.
    """
    if update is None:
        return current or []
    return update


class GraphState(TypedDict, total=False):
    """
    The LangGraph State (The Blackboard).
    
    Coordinates multiple modalities (SQL + Vector RAG + Session Identity),
    preserves additive multi-turn working memory, and controls cyclic validation loops.
    Strictly partitions state into Persistent, Managed, and Ephemeral scopes
    to prevent VRAM/RAM exhaustion and context-window bloat.
    """
    # =========================================================================
    # 1. PERSISTENT STATE (Preserved across turns in Checkpointer / Blackboard)
    # =========================================================================
    session_id: str
    summary: str  # Condensed historical memory across older conversation turns
    user_context: Dict[str, Any]  # Tenant and user authentication metadata
    user_metadata: Dict[str, Any]  # Profile and tenant metadata
    working_memory: Annotated[Dict[str, Any], merge_working_memory]

    # =========================================================================
    # 2. MANAGED STATE (Bounded window, pruned to last 2-4 entries)
    # =========================================================================
    messages: Annotated[List[Dict[str, Any]], manage_messages]

    # =========================================================================
    # 3. EPHEMERAL STATE (Pruned / Cleared after each turn by Janitor node)
    # =========================================================================
    raw_query: str
    condensed_query: str
    current_query_analysis: Optional[Dict[str, Any]]

    # Multimodal Retrieved Context (Overwritten per turn, never accumulated)
    retrieved_chunks: Annotated[List[Dict[str, Any]], merge_context_list]
    sql_result_rows: Annotated[List[Dict[str, Any]], merge_context_list]
    retrieved_context: Annotated[List[Dict[str, Any]], merge_context_list]

    # Strategy & Routing
    routing_decision: Optional[RoutingDecision]
    target_datasource: Optional[str]

    # SQL Execution & Human-in-the-Loop Breakpoint
    generated_sql: Optional[str]
    sql_approval_status: Optional[str]  # "pending", "approved", "rejected"

    # Grounding & Synthesis
    synthesized_response: str
    grounding_report: Dict[str, Any]  # { "is_grounded": bool, "confidence": float, "flags": dict }

    # Cyclic Correction Loop Control
    iteration_count: int
    correction_instruction: Optional[str]

    # Multi-Tenant Session & Audit
    error_message: Optional[str]
    audit_meta: Dict[str, Any]
