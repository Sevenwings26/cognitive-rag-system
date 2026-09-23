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

    REFERENTIAL_PATTERN = re.compile(
        r"\b(he|him|his|she|her|hers|it|its|they|them|their|theirs|"
        r"this|that|these|those|same|previous|prior|above|latter|former|"
        r"what about|how about|and the|show his|find his|check his|get his|list his|"
        r"any loans|any accounts|any transactions|does he|did he|is he|has he|"
        r"the customer|this customer|that customer|same customer)\b",
        re.IGNORECASE
    )

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
        "Your task is to rewrite a user's follow-up question into a single, self-contained, standalone question by resolving all pronouns, ellipses, and ambiguous references using the provided conversation history and known entities.\n\n"
        "RULES:\n"
        "1. Replace pronouns ('he', 'his', 'she', 'her', 'it', 'they', 'this customer', 'that document') with explicit names and relevant entity context (e.g., 'customer Adebayo Adekunle (customer_id: 1008)').\n"
        "2. Do NOT dump past search keys (such as previous BVN numbers or past query terms) into the rewritten question if they are not relevant to the new question. Keep the rewritten question natural and focused.\n"
        "3. Preserve the exact intent, questions, system names, and domain concepts requested by the user. Do NOT modify terms like 'lending system', 'accounting', 'payments', 'core banking', etc. Do not answer the question; only rewrite it.\n"
        "4. Output ONLY the standalone rewritten question. Do not include quotes, markdown fences, or conversational filler.\n"
        "5. CRITICAL: NEVER insert or hallucinate specific database backend or schema names (e.g. noros_customer_db, noros_lending_db, postgres, mssql) into the rewritten question unless the user explicitly named them in their query. Retain the user's natural language domain terms (e.g., 'in our lending system', 'credit facilities', 'loans').\n"
        "6. TOPIC SHIFTS & EXCLUSIONS: When the user asks an exclusionary or aggregate question (e.g., 'Apart from X, which other industries...', 'Which other organization are we serving?'), do NOT attach individual person names or customer IDs. Rewrite as a clear categorical inquiry regarding corporate employers or organizations (e.g., 'Apart from Dangote, which other corporate employers or industries do our customers belong to?').\n"
        "7. SYSTEM INQUIRIES: If the user asks about system architecture, data sources, or connectors (e.g., 'How many data sources do we have on this system?'), NEVER bind customer IDs, BVNs, or banking entities. Keep the inquiry focused strictly on the system data sources."
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
