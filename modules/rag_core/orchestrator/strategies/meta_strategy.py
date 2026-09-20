# modules/rag_core/orchestrator/strategies/meta_strategy.py
import logging
from typing import List, Dict, Any, Optional, Tuple, Iterator
from sqlalchemy.orm import Session

from modules.auth.domain.tokens import TokenData
from modules.governance.domain.models import IngestionJob, EnterpriseDocument
from modules.rag_core.orchestrator.strategies.base import BaseRetrievalStrategy

logger = logging.getLogger("meta_strategy")

class SystemMetaStrategy(BaseRetrievalStrategy):
    """
    Route: System Metadata & Knowledge Source Audit Strategy.
    Enforces strict Role-Based Access Control (RBAC):
    - Administrative users (SUPER_ADMIN, SYSTEM_ADMIN): Authoritative inventory of connected
      enterprise databases, connectors, and knowledge repositories.
    - Standard users (MEMBER, DEPT_ADMIN, GUEST): Security-governed redirection preventing
      infrastructure enumeration and information disclosure.
    """

    ADMIN_ROLES = {"SUPER_ADMIN", "SYSTEM_ADMIN", "ADMIN"}

    GOVERNANCE_RESTRICTION_MESSAGE = (
        "For security and governance reasons, details regarding internal data sources and "
        "infrastructure architecture are restricted. Please contact your organization's "
        "technical or system administration team for inquiries regarding connected enterprise "
        "knowledge sources."
    )

    def execute_stream(
        self,
        query: str,
        user_context: TokenData,
        db: Optional[Session] = None,
        session_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        template_id: Optional[str] = None,
        top_k: int = 3,
        score_threshold: float = 0.35,
        mode: str = "auto",
        **kwargs
    ) -> Iterator[Tuple[str, Any]]:
        yield ("status", {
            "step": "system_meta_audit",
            "stage": "retrieval",
            "title": "Auditing Data Source Registry",
            "details": "Validating RBAC permissions and querying connected enterprise data sources...",
            "status": "in_progress"
        })

        user_role = (user_context.role or "").upper()
        if hasattr(user_context.role, "value"):
            user_role = user_context.role.value.upper()

        # 1. RBAC Check: Non-admins receive governance restriction message
        if user_role not in self.ADMIN_ROLES:
            logger.info(f"[SYSTEM META] Access restricted for user role '{user_role}'. Returning governance redirection.")
            yield ("status", {
                "step": "system_meta_audit",
                "stage": "retrieval",
                "title": "Access Restricted (RBAC)",
                "details": "Standard user access restricted under governance policy.",
                "status": "completed"
            })
            yield ("delta", {"content": self.GOVERNANCE_RESTRICTION_MESSAGE})
            yield ("result", (self.GOVERNANCE_RESTRICTION_MESSAGE, [], True, 1.0))
            return

        # 2. Admin Inventory: Audit all registered knowledge sources for the tenant
        if not db:
            msg = "System data source registry is currently unavailable."
            yield ("delta", {"content": msg})
            yield ("result", (msg, [], False, 0.0))
            return

        jobs = db.query(IngestionJob).filter(IngestionJob.org_id == user_context.org_id).all()

        # Categorize registered knowledge sources
        db_sources = []
        connector_sources = []
        for j in jobs:
            source_type = (j.source_type or "").upper()
            if any(kw in source_type for kw in ("DB", "POSTGRES", "MYSQL", "MSSQL", "ORACLE")):
                db_sources.append({
                    "name": j.name,
                    "type": j.source_type,
                    "status": j.status.value if hasattr(j.status, "value") else str(j.status)
                })
            else:
                connector_sources.append({
                    "name": j.name,
                    "type": j.source_type,
                    "status": j.status.value if hasattr(j.status, "value") else str(j.status)
                })

        # Count unstructured enterprise documents
        unstructured_doc_count = db.query(EnterpriseDocument).filter(
            EnterpriseDocument.org_id == user_context.org_id,
            ~EnterpriseDocument.filename.like("schema_%")
        ).count()

        total_registered_sources = len(jobs)

        lines = [
            f"The system has **{total_registered_sources} primary enterprise knowledge sources** registered and active for your organization:\n",
            "### 1. Structured Relational Databases:"
        ]
        for s in db_sources:
            lines.append(f"- **{s['name']}** (Dialect/Type: `{s['type']}`, Status: `{s['status']}`)")

        if connector_sources:
            lines.append("\n### 2. External Cloud & Document Connectors:")
            for s in connector_sources:
                lines.append(f"- **{s['name']}** (Type: `{s['type']}`, Status: `{s['status']}`)")

        lines.append("\n### 3. Unstructured Enterprise Document Knowledge Base:")
        lines.append(f"- **Enterprise Vector Repository**: {unstructured_doc_count} active indexed documents (policies, manuals, operational files).")

        answer = "\n".join(lines)
        sources = [
            {
                "source_name": "System Data Source Registry",
                "type": "system_meta",
                "count": total_registered_sources,
                "databases": [s["name"] for s in db_sources],
                "connectors": [s["name"] for s in connector_sources],
                "unstructured_documents": unstructured_doc_count
            }
        ]

        yield ("status", {
            "step": "system_meta_audit",
            "stage": "retrieval",
            "title": "Data Source Registry Audited",
            "details": f"Discovered {total_registered_sources} registered enterprise knowledge sources.",
            "status": "completed"
        })

        # Stream the inventory tokens in chunks
        chunk_size = 40
        for i in range(0, len(answer), chunk_size):
            yield ("delta", {"content": answer[i:i + chunk_size]})

        yield ("result", (answer, sources, True, 1.0))

    def execute(
        self,
        query: str,
        user_context: TokenData,
        db: Optional[Session] = None,
        session_id: Optional[str] = None,
        persona_id: Optional[str] = None,
        template_id: Optional[str] = None,
        top_k: int = 3,
        score_threshold: float = 0.35,
        mode: str = "auto",
        **kwargs
    ) -> Tuple[str, List[Dict[str, Any]], bool, float]:
        """Synchronous wrapper consuming execute_stream for backward compatibility."""
        stream = self.execute_stream(
            query=query,
            user_context=user_context,
            db=db,
            session_id=session_id,
            persona_id=persona_id,
            template_id=template_id,
            top_k=top_k,
            score_threshold=score_threshold,
            mode=mode,
            **kwargs
        )
        for item_type, data in stream:
            if item_type == "result":
                return data
        return ("", [], True, 1.0)

