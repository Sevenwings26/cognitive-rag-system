# modules/rag_core/retrieval/security_filter.py
from typing import Optional, List
from qdrant_client.models import Filter, FieldCondition, MatchValue, MatchAny
from modules.auth.domain.models import UserRole

class RAGSecurityFilterBuilder:
    @staticmethod
    def build_search_filter(
        org_id: str,
        department_id: Optional[str] = None,
        user_id: Optional[str] = None,
        user_role: Optional[UserRole] = UserRole.MEMBER,
        session_id: Optional[str] = None,
        scope: Optional[str] = None
    ) -> Filter:
        tenant_condition = FieldCondition(key="org_id", match=MatchValue(value=org_id))
        or_clauses = []

        if scope == "session" and session_id:
            return Filter(
                must=[
                    tenant_condition,
                    FieldCondition(key="session_id", match=MatchValue(value=session_id)),
                    FieldCondition(key="scope", match=MatchValue(value="session"))
                ]
            )

        if scope == "personal":
            if user_id:
                or_clauses.append(FieldCondition(key="uploader_id", match=MatchValue(value=user_id)))
            if session_id:
                or_clauses.append(FieldCondition(key="session_id", match=MatchValue(value=session_id)))
            return Filter(must=[tenant_condition], should=or_clauses if or_clauses else None)

        # Enterprise Knowledge Search: Strictly exclude in-chat session documents to guarantee zero bleed
        enterprise_must_not = [FieldCondition(key="scope", match=MatchValue(value="session"))]

        if user_role == UserRole.SUPER_ADMIN:
            return Filter(
                must=[tenant_condition],
                must_not=enterprise_must_not
            )

        # 1. Public Documents
        or_clauses.append(FieldCondition(key="access_level", match=MatchValue(value="PUBLIC")))

        # 2. User Uploads (Only if authenticated)
        if user_id:
            or_clauses.append(FieldCondition(key="uploader_id", match=MatchValue(value=user_id)))

        # 3. Department Documents (Only if department_id provided)
        if department_id:
            dept_acl_allowed = ["DEPARTMENT"]
            if user_role == UserRole.DEPT_ADMIN:
                dept_acl_allowed.append("CONFIDENTIAL")

            dept_clause = Filter(
                must=[
                    FieldCondition(key="department_id", match=MatchValue(value=department_id)),
                    FieldCondition(key="access_level", match=MatchAny(any=dept_acl_allowed))
                ]
            )
            or_clauses.append(dept_clause)

        return Filter(
            must=[tenant_condition],
            must_not=enterprise_must_not,
            should=or_clauses if or_clauses else None
        )

    # Class-level alias for convenient calling
    build = build_search_filter

