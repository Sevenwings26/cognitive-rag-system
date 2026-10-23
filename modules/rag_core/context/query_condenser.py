# modules/rag_core/context/query_condenser.py
import re
import logging
from typing import List, Dict, Any, Optional

from modules.rag_core.context.models import SessionWorkingMemory, EntityScope
from modules.rag_core.providers.llm import BaseLLMService

logger = logging.getLogger("query_condenser")

class QueryCondenser:
    """
    Universal Conversational Query Condenser & Anaphora Resolver.
    Detects referential pronouns, ellipses, and cross-turn references,
    rewriting follow-up prompts into self-contained canonical queries with explicit entity bindings.
    """

    UNIVERSAL_REFERENTIAL_PATTERN = re.compile(
        r"\b("
        r"he|him|his|she|her|hers|it|its|they|them|their|theirs|"
        r"this|that|these|those|same|previous|prior|above|latter|former|"
        r"what about|how about|and the|show|find|check|get|list|"
        r"does (?:he|she|it|they)|did (?:he|she|it|they)|is (?:he|she|it|they)|has (?:he|she|it|they)|"
        r"the same|that same|this same|that one|this one"
        r")\b",
        re.IGNORECASE
    )
    REFERENTIAL_PATTERN = UNIVERSAL_REFERENTIAL_PATTERN

    TOPIC_SHIFT_PATTERN = re.compile(
        r"\b(apart from|other than|besides|excluding|which other|what other|which else|what else|across all|in general|overall)\b",
        re.IGNORECASE
    )

    SYSTEM_META_PATTERN = re.compile(
        r"\b(data sources?|connected databases?|databases? (?:registered|connected|available)|how many sources|what systems|registered connectors|on this system|in this system)\b",
        re.IGNORECASE
    )

    SYSTEM_INSTRUCTION = (
        "You are an expert conversational disambiguation engine and anaphora resolver for enterprise data and RAG systems.\n"
        "Your task is to rewrite a user's follow-up question into a single, self-contained, canonical question "
        "by resolving all pronouns, ellipses, and ambiguous references using the provided conversation history, known entities, and document context.\n\n"
        "RULES:\n"
        "1. Replace pronouns ('he', 'his', 'she', 'her', 'it', 'they', 'this', 'that', 'that record', 'that document') with explicit names and known entity identifiers from Known Entities (e.g. 'subscriber John Doe (msisdn: 08012345678)', 'patient Alice Smith (mrn: 10492)', 'order ORD-9921', 'document travel_policy.pdf'). If an attribute is not present in Known Entities, do NOT fabricate an unknown ID; simply omit unknown attributes.\n"
        "2. Do NOT dump past search keys or previous turn queries into the rewritten question if they are not relevant to the new question. Keep the rewritten question natural and focused.\n"
        "3. Preserve the exact intent, questions, system names, cloud paths, and domain concepts requested by the user. Do not answer the question; only rewrite it.\n"
        "4. Output ONLY the standalone rewritten question. Do not include quotes, markdown fences, or conversational filler.\n"
        "5. CRITICAL: NEVER insert or hallucinate specific database backend or schema names (e.g. postgres, mssql, internal DB names) into the rewritten question unless the user explicitly named them in their query. Retain the user's natural language domain terms.\n"
        "6. TOPIC SHIFTS & EXCLUSIONS: When the user asks an exclusionary or aggregate question (e.g., 'Apart from X, which other...', 'In general across all...'), do NOT attach individual person names or specific entity IDs. Rewrite as a clear categorical inquiry.\n"
        "7. SYSTEM INQUIRIES: If the user asks about system architecture, data sources, connectors, or cloud repositories (e.g., 'How many data sources do we have on this system?'), NEVER bind specific record entity IDs. Keep the inquiry focused strictly on the system data sources.\n"
        "8. MULTI-SOURCE & CLOUD DOCUMENTS: When the user refers to an uploaded document, cloud storage path (S3, SharePoint, Google Drive), spreadsheet, vendor, or partner, resolve references to the specific document filename, cloud repository, or external entity named in the conversation history.\n"
        "9. IDENTIFIER INTEGRITY: Never alter, truncate, or pad digits to any numeric or code identifier (e.g. MSISDN, MRN, VIN, BVN, UUID)."
    )

    @classmethod
    def should_condense(
        cls,
        query: str,
        history: List[Dict[str, str]],
        memory: SessionWorkingMemory
    ) -> bool:
        """
        Fast-path heuristic: determines whether query rewriting is necessary.
        Bypasses LLM rewriting for first-turn queries and unambiguous self-contained prompts (0 ms overhead).
        """
        # Pure conversational tokens / acknowledgements should never be rewritten
        if re.match(r"^\s*(?:alright|all right|okay|ok|cool|great|got it|sure|noted|yes|no|yeah|yep|nope|fine|understood|thanks|thank you|hi|hello|bye|[,\s.!-]+)+$", query, re.IGNORECASE):
            return False

        # If no history and no active entities/names in memory, no context exists to resolve
        if not history and not memory.active_entities and not memory.active_names:
            return False

        # If referential token is present, we must condense
        if cls.REFERENTIAL_PATTERN.search(query):
            return True

        # If topic shift or system meta inquiry, condense to ensure clean categorical query
        if cls.TOPIC_SHIFT_PATTERN.search(query) or cls.SYSTEM_META_PATTERN.search(query):
            return True

        # Elliptical starters like "What about...", "And for...", "Any..."
        clean_q = query.strip().lower()
        if clean_q.startswith(("what about", "how about", "and ", "any ", "does ", "is he", "has he")):
            return True

        # If query is short (e.g. <= 4 words) and history exists, likely an ellipsis
        words = query.strip().split()
        if len(words) <= 4 and (history or memory.active_entities):
            return True

        return False

    @classmethod
    def condense(
        cls,
        query: str,
        history: List[Dict[str, str]],
        memory: SessionWorkingMemory,
        llm_service: BaseLLMService
    ) -> str:
        """
        Rewrites a multi-turn query into a canonical standalone query.
        If no referential resolution is needed, returns original query instantly.
        """
        if not cls.should_condense(query, history, memory):
            return query

        try:
            # Determine target scope based on query characteristics
            clean_q = query.strip().lower()
            target_scope = EntityScope.INDIVIDUAL

            if cls.SYSTEM_META_PATTERN.search(clean_q):
                target_scope = EntityScope.SYSTEM_META
                memory.evict_for_topic_shift(EntityScope.SYSTEM_META)
            elif cls.TOPIC_SHIFT_PATTERN.search(clean_q):
                target_scope = EntityScope.AGGREGATE
                memory.evict_for_topic_shift(EntityScope.AGGREGATE)

            # Format history turns (sanitizing technical table dumps and DB source names)
            history_lines = []
            for msg in history[-6:]:
                role = "User" if msg.get("role") == "user" else "Assistant"
                content = msg.get("content", "").strip()
                # Sanitize technical source lines and tables so LLM doesn't latch onto internal DB names
                content = re.sub(r"Sources:\s*\[.*?\]", "", content)
                content = re.sub(r"\*\*Database Query Results:\*\*.*", "", content, flags=re.DOTALL)
                content = re.sub(r"\|[^\n]+\|", "", content)
                content = re.sub(r"\n{2,}", "\n", content).strip()
                if len(content) > 300:
                    content = content[:300] + "..."
                if content:
                    history_lines.append(f"{role}: {content}")

            history_text = "\n".join(history_lines) if history_lines else "None"

            is_external_or_doc_inquiry = any(term in clean_q for term in ["vendor", "supplier", "provider", "partner", "document", "file", "policy", "s3", "sharepoint", "drive", "cloud"])
            if is_external_or_doc_inquiry:
                ext_lines = []
                ext_list = getattr(memory, "active_external_entities", None) or getattr(memory, "active_vendors", None)
                if ext_list:
                    ext_lines.append(f"Referenced External Entities / Partners: {', '.join(ext_list)}")
                if memory.active_documents:
                    ext_lines.append(f"Referenced Documents: {', '.join(memory.active_documents)}")
                if getattr(memory, "active_cloud_sources", None):
                    ext_lines.append(f"Referenced Cloud Sources: {', '.join(memory.active_cloud_sources)}")
                memory_context = "\n".join(ext_lines) if ext_lines else "None"
            else:
                memory_context = memory.get_summary_context(target_scope) or "None"

            prompt = (
                f"=== Known Entities / Working Memory ===\n{memory_context}\n\n"
                f"=== Recent Conversation History ===\n{history_text}\n\n"
                f"=== User Follow-Up Question to Disambiguate ===\n{query}\n\n"
                f"Rewrite the user's follow-up question into a complete, standalone question with explicit entity identifiers:"
            )

            if hasattr(llm_service, "generate_text"):
                rewritten = llm_service.generate_text(
                    prompt=prompt,
                    system_instruction=cls.SYSTEM_INSTRUCTION
                )
            elif hasattr(llm_service, "generate_answer"):
                rewritten = llm_service.generate_answer(
                    system_instruction=cls.SYSTEM_INSTRUCTION,
                    user_prompt=prompt
                )
            else:
                return query

            cleaned = rewritten.strip().strip('"').strip("'").strip()
            # Remove markdown backticks if any
            if cleaned.startswith("```"):
                cleaned = re.sub(r"^```(?:text)?\s*", "", cleaned)
                cleaned = re.sub(r"\s*```$", "", cleaned).strip()

            if cleaned and len(cleaned) > 5 and not cleaned.lower().startswith("i cannot"):
                logger.info(f"[QUERY CONDENSER] Disambiguated: '{query}' -> '{cleaned}'")
                return cleaned

            return query
        except Exception as e:
            logger.warning(f"[QUERY CONDENSER] Failed to rewrite query: {e}. Using original.")
            return query
