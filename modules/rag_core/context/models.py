# modules/rag_core/context/models.py
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional
from enum import Enum
import json
from pydantic import BaseModel, Field

class ActionType(str, Enum):
    """Categorization for proactive next best actions."""
    PROMPT_SUGGESTION = "PROMPT_SUGGESTION"   # User-facing prompt suggestion
    WORKFLOW_STEP = "WORKFLOW_STEP"           # Progressive next stage in business workflow
    CLARIFICATION = "CLARIFICATION"           # Disambiguation prompt when uncertainty exists

class SuggestedAction(BaseModel):
    """
    Structured Next Best Action proposed by the proactive cognition engine.
    """
    label: str = Field(..., description="Short button/chip title for the action UI")
    suggested_prompt: str = Field(..., description="The executable canonical prompt triggered when selected")
    conversational_prompt: Optional[str] = Field(None, description="Natural, conversational consultative phrasing")
    action_type: ActionType = Field(default=ActionType.PROMPT_SUGGESTION, description="Action category")
    target_strategy: Optional[str] = Field(None, description="Expected strategy route e.g. 'sql', 'enterprise'")
    confidence: float = Field(default=1.0, description="Heuristic or model confidence score (0.0 - 1.0)")

class EntityScope(str, Enum):
    """Conversation scope levels governing context injection and entity eviction."""
    INDIVIDUAL = "INDIVIDUAL"     # Specific person or account (e.g. Turn 1 customer lookup)
    COLLECTION = "COLLECTION"     # Multi-entity group/filter (e.g. Dangote employees)
    AGGREGATE = "AGGREGATE"       # Global statistics / categories (e.g. distinct industries/employers)
    SYSTEM_META = "SYSTEM_META"   # Platform inspection (e.g. how many data sources registered)

@dataclass
class SessionWorkingMemory:
    """
    Universal blackboard working memory maintained per session thread.
    Accumulates resolved entities, identifiers, quantitative metrics, and source citations
    across all modalities (SQL, Enterprise RAG, and Scoped Session Documents).
    """
    session_id: str
    active_entities: Dict[str, Any] = field(default_factory=dict)     # e.g. {"msisdn": "...", "patient_mrn": "...", "customer_id": ...}
    active_names: List[str] = field(default_factory=list)             # e.g. ["Adebayo Adekunle", "Dr. Gregory House"]
    active_documents: List[str] = field(default_factory=list)         # e.g. ["expense_policy.docx", "patient_history.pdf"]
    active_cloud_sources: List[str] = field(default_factory=list)     # e.g. ["s3://telecom-cdr", "sharepoint://clinical-trials"]
    active_vendors: List[str] = field(default_factory=list)           # Referenced external vendors / partners (backward compatibility)
    active_external_entities: List[str] = field(default_factory=list) # Generalized external entities (vendors, facilities, carriers)
    active_metrics: Dict[str, Any] = field(default_factory=dict)      # e.g. {"balance": "88450000.00", "latency_ms": "12.4"}
    active_tables: List[str] = field(default_factory=list)            # Relational tables touched in conversation
    last_target_database: Optional[str] = None                        # e.g. "ehr_db", "cdr_db", "noros_customer_db"
    last_strategy: Optional[str] = None                               # e.g. "sql", "enterprise", "session", "system_meta"
    turn_count: int = 0
    scope: str = EntityScope.INDIVIDUAL.value                         # Current conversational entity scope
    active_topic: Optional[str] = None                               # Topic indicator
    primary_anchor_id: Optional[Any] = None                           # Singular anchor ID to avoid multi-row overwrite drift
    collection_ids: List[Any] = field(default_factory=list)           # List of entity IDs in a collection/multi-row scope
    suggested_actions: List[Dict[str, Any]] = field(default_factory=list) # Proactive next best actions

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionWorkingMemory":
        ext_entities = data.get("active_external_entities") or data.get("active_vendors") or []
        return cls(
            session_id=data.get("session_id", ""),
            active_entities=data.get("active_entities") or {},
            active_names=data.get("active_names") or [],
            active_documents=data.get("active_documents") or [],
            active_cloud_sources=data.get("active_cloud_sources") or [],
            active_vendors=ext_entities,
            active_external_entities=ext_entities,
            active_metrics=data.get("active_metrics") or {},
            active_tables=data.get("active_tables") or [],
            last_target_database=data.get("last_target_database"),
            last_strategy=data.get("last_strategy"),
            turn_count=data.get("turn_count", 0),
            scope=data.get("scope", EntityScope.INDIVIDUAL.value),
            active_topic=data.get("active_topic"),
            primary_anchor_id=data.get("primary_anchor_id"),
            collection_ids=data.get("collection_ids") or [],
            suggested_actions=data.get("suggested_actions") or []
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, json_str: str) -> "SessionWorkingMemory":
        try:
            return cls.from_dict(json.loads(json_str))
        except Exception:
            return cls(session_id="")

    def evict_for_topic_shift(self, new_scope: EntityScope) -> None:
        """
        Partially resets or evicts stale entity bindings when an intentional topic shift occurs.
        Prevents single-entity IDs from contaminating aggregate or system-level queries.
        """
        self.scope = new_scope.value if isinstance(new_scope, EntityScope) else str(new_scope)
        if new_scope in (EntityScope.AGGREGATE, EntityScope.SYSTEM_META):
            # Suppress individual ID anchors to avoid Frankenstein entity binds
            self.active_entities.clear()
            self.active_names.clear()
            self.collection_ids.clear()
            self.primary_anchor_id = None

    def get_summary_context(self, target_scope: Optional[EntityScope] = None) -> str:
        """
        Formats an entity summary for LLM prompt injection, scoped to the current inquiry.
        Suppresses single-entity IDs when the target scope is AGGREGATE or SYSTEM_META.
        """
        effective_scope = target_scope.value if isinstance(target_scope, EntityScope) else (target_scope or self.scope)
        
        # System meta queries must receive zero business entity context
        if effective_scope == EntityScope.SYSTEM_META.value:
            return ""

        parts = []
        ext_list = self.active_external_entities or self.active_vendors
        if ext_list:
            parts.append(f"Referenced External Entities / Partners: {', '.join(ext_list)}")

        if self.active_cloud_sources:
            parts.append(f"Referenced Cloud Sources / Repositories: {', '.join(self.active_cloud_sources)}")

        if effective_scope != EntityScope.AGGREGATE.value and self.active_entities:
            ent_strs = [f"{k}: {v}" for k, v in self.active_entities.items()]
            parts.append(f"Known Entities: {', '.join(ent_strs)}")

        if effective_scope != EntityScope.AGGREGATE.value and self.active_names:
            parts.append(f"Known Names: {', '.join(self.active_names)}")

        if effective_scope != EntityScope.AGGREGATE.value and self.collection_ids:
            parts.append(f"Referenced Collection IDs: {', '.join(str(c) for c in self.collection_ids)}")

        if self.active_metrics:
            met_strs = [f"{k}: {v}" for k, v in self.active_metrics.items()]
            parts.append(f"Known Metrics/Values: {', '.join(met_strs)}")

        if self.active_documents:
            parts.append(f"Referenced Documents: {', '.join(self.active_documents)}")

        return "\n".join(parts)

