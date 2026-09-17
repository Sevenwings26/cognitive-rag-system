# modules/rag_core/orchestrator/query_planner.py
import re
from typing import List, Optional
from modules.auth.domain.tokens import TokenData
from modules.rag_core.domain.types import QueryPlan

QueryExecutionPlan = QueryPlan

class QueryPlanner:
    CONVERSATIONAL_PATTERNS = [
        r"^\s*(hi|hello|hey|good morning|good afternoon|good evening|howdy|greetings)\b",
        r"^\s*(who are you|what can you do|help me|how does this work|what is your name)\b",
        r"^\s*(thank you|thanks|bye|goodbye|see you|good night)\b",
        r"^\s*(how are you|how's it going|what's up)\b"
    ]

    STRUCTURED_SQL_KEYWORDS = [
        r"\b(how many|total|sum|count|average|highest|lowest|minimum|maximum|min|max|revenue|invoices?|orders?|records?|salary|salaries|database|rows?|tables?|aggregate|metrics?)\b",
        r"\b(bvn|nin|ssn|tin|iban|cif|swift)\b",
        r"\b(customer|customers|account|accounts|client|clients|beneficiar(?:y|ies)|transaction|transactions|transfer|transfers|loan|loans|repayment|repayments|collateral|balance|balances|merchant|merchants|pos|atm|branch|branches|employee|employees|kyc|aml|sanctions|fraud)\b",
        r"^\s*(who is|find customer|lookup|verify|verification status|get details|check account|which customer|customer associated with)\b"
    ]

    DOCUMENT_RAG_KEYWORDS = [
        r"\b(policy|policies|document|documents|file|files|uploaded|pdf|handbook|manual|contract|procedure|agreement|revenue|q[1-4]|report|internal|nda|org|department|guideline|compliance|sop)\b"
    ]

    GENERAL_KNOWLEDGE_PATTERNS = [
        r"^\s*(what is the capital of|who wrote|when was|translate|calculate|write a|code a|explain the concept of)\b",
        r"\b(continents?|planets?|oceans?|countries|pythagorean|fibonacci|javascript|python|css|html)\b"
    ]

    @classmethod
    def analyze_and_plan(
        cls,
        query: str,
        user_context: Optional[TokenData] = None,
        has_session_documents: bool = False,
        mode: str = "auto"
    ) -> QueryPlan:
        clean_query = query.strip()
        lower_query = clean_query.lower()

        is_doc_intent = any(re.search(p, lower_query) for p in cls.DOCUMENT_RAG_KEYWORDS)
        has_sql_aggregate = any(k in lower_query for k in ["how many", "count", "sum", "average", "total", "select", "rows", "table"])
        is_sql_intent = any(re.search(p, lower_query) for p in cls.STRUCTURED_SQL_KEYWORDS) and (not is_doc_intent or has_sql_aggregate)

        # 1. Explicit Mode Override
        if mode == "general":
            return QueryPlan(
                is_conversational_only=True,
                target_scopes=[],
                sub_queries=[clean_query],
                intent_category="CONVERSATIONAL",
                is_structured_sql=False
            )
        elif mode in ("sql", "database"):
            return QueryPlan(
                is_conversational_only=False,
                target_scopes=["enterprise"],
                sub_queries=[clean_query],
                intent_category="STRUCTURED_SQL",
                is_structured_sql=True
            )
        elif mode == "rag":
            return QueryPlan(
                is_conversational_only=False,
                target_scopes=["personal", "department", "enterprise"],
                sub_queries=[clean_query],
                intent_category="STRUCTURED_SQL" if is_sql_intent else "DOCUMENT_RAG",
                is_structured_sql=is_sql_intent
            )

        # 2. Dynamic Auto-Routing:
        # A. Conversational / Greetings
        for pattern in cls.CONVERSATIONAL_PATTERNS:
            if re.search(pattern, lower_query):
                return QueryPlan(
                    is_conversational_only=True,
                    target_scopes=[],
                    sub_queries=[clean_query],
                    intent_category="CONVERSATIONAL",
                    is_structured_sql=False
                )

        is_doc_intent = any(re.search(p, lower_query) for p in cls.DOCUMENT_RAG_KEYWORDS)

        # B. Document-referencing inquiries (policies, handbooks, procedures, contracts) -> Prioritize RAG
        if is_doc_intent and not any(k in lower_query for k in ["how many", "count", "sum", "average", "total", "select", "rows", "table"]):
            return QueryPlan(
                is_conversational_only=False,
                target_scopes=["personal", "department", "enterprise"],
                sub_queries=[clean_query],
                intent_category="DOCUMENT_RAG",
                is_structured_sql=False
            )

        # C. Structured SQL / Tabular Queries
        if is_sql_intent:
            return QueryPlan(
                is_conversational_only=False,
                target_scopes=["personal", "department", "enterprise"],
                sub_queries=[clean_query],
                intent_category="STRUCTURED_SQL",
                is_structured_sql=True
            )

        # D. Document-referencing keywords fallback
        if is_doc_intent:
            return QueryPlan(
                is_conversational_only=False,
                target_scopes=["personal", "department", "enterprise"],
                sub_queries=[clean_query],
                intent_category="DOCUMENT_RAG",
                is_structured_sql=False
            )

        # D. If session has uploaded files attached -> Route to RAG
        if has_session_documents:
            return QueryPlan(
                is_conversational_only=False,
                target_scopes=["personal", "department", "enterprise"],
                sub_queries=[clean_query],
                intent_category="DOCUMENT_RAG",
                is_structured_sql=False
            )

        # E. General world knowledge pattern check
        for pattern in cls.GENERAL_KNOWLEDGE_PATTERNS:
            if re.search(pattern, lower_query):
                return QueryPlan(
                    is_conversational_only=True,
                    target_scopes=[],
                    sub_queries=[clean_query],
                    intent_category="GENERAL_KNOWLEDGE",
                    is_structured_sql=False
                )

        # Default fallback: Route to RAG
        return QueryPlan(
            is_conversational_only=False,
            target_scopes=["personal", "department", "enterprise"],
            sub_queries=[clean_query],
            intent_category="DOCUMENT_RAG",
            is_structured_sql=False
        )
