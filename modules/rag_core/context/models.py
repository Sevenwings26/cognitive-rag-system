# modules/rag_core/context/models.py
from dataclasses import dataclass, field, asdict
from typing import Dict, Any, List, Optional
import json

@dataclass
class SessionWorkingMemory:
    """
    Universal blackboard working memory maintained per session thread.
    Accumulates resolved entities, identifiers, quantitative metrics, and source citations
    across all modalities (SQL, Enterprise RAG, and Scoped Session Documents).
    """
    session_id: str
    active_entities: Dict[str, Any] = field(default_factory=dict)     # e.g. {"customer_id": 1008, "bvn": "90000001008", "account_number": "0123456708", "loan_id": 3008}
    active_names: List[str] = field(default_factory=list)             # e.g. ["Adebayo Adekunle"]
    active_documents: List[str] = field(default_factory=list)         # e.g. ["expense_policy.docx"]
    active_metrics: Dict[str, Any] = field(default_factory=dict)      # e.g. {"balance": "88450000.00", "interest_rate": "13.00"}
    last_target_database: Optional[str] = None                        # e.g. "noros_customer_db", "noros_core_banking_db", "noros_lending_db"
    last_strategy: Optional[str] = None                               # e.g. "sql", "enterprise", "session", "conversational"
    turn_count: int = 0

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: Dict[str, Any]) -> "SessionWorkingMemory":
        return cls(
            session_id=data.get("session_id", ""),
            active_entities=data.get("active_entities") or {},
            active_names=data.get("active_names") or [],
            active_documents=data.get("active_documents") or [],
            active_metrics=data.get("active_metrics") or {},
            last_target_database=data.get("last_target_database"),
            last_strategy=data.get("last_strategy"),
            turn_count=data.get("turn_count", 0)
        )

    def to_json(self) -> str:
        return json.dumps(self.to_dict())

    @classmethod
    def from_json(cls, json_str: str) -> "SessionWorkingMemory":
        try:
            return cls.from_dict(json.loads(json_str))
        except Exception:
            return cls(session_id="")

    def get_summary_context(self) -> str:
        """Formats an unambiguous entity summary for LLM prompt injection."""
        parts = []
        if self.active_entities:
            ent_strs = [f"{k}: {v}" for k, v in self.active_entities.items()]
            parts.append(f"Known Entities: {', '.join(ent_strs)}")
        if self.active_names:
            parts.append(f"Known Names: {', '.join(self.active_names)}")
        if self.active_metrics:
            met_strs = [f"{k}: {v}" for k, v in self.active_metrics.items()]
            parts.append(f"Known Metrics/Values: {', '.join(met_strs)}")
        if self.active_documents:
            parts.append(f"Referenced Documents: {', '.join(self.active_documents)}")
        return "\n".join(parts)

