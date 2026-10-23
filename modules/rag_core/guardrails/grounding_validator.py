# modules/rag_core/guardrails/grounding_validator.py
import logging
import re
from typing import List, Dict, Any, Tuple

logger = logging.getLogger("grounding_validator")

class GroundingValidator:
    STRICT_SYSTEM_INSTRUCTION = (
        "You are an enterprise AI knowledge assistant for {{ org_name }}.\n\n"
        "RESPONSE GUIDELINES:\n"
        "- Deliver direct, fluent, professional, and neatly formatted answers (use bold highlights, clean lists, or tables where appropriate).\n"
        "- Do NOT use repetitive robotic meta-narration (e.g. NEVER say 'According to [Source 1]...', 'Based on the provided documents...', or 'This information is derived from the spreadsheet'). Present the facts directly and naturally.\n"
        "- All factual assertions regarding {{ org_name }} must be accurately grounded in the supplied context snippets.\n"
        "- If the context does not contain sufficient details to answer, state clearly and concisely that internal organizational records do not contain this information."
    )

    ADAPTIVE_SYSTEM_INSTRUCTION = (
        "You are an intelligent Enterprise AI Knowledge & Cognitive Assistant for {{ org_name }}.\n\n"
        "RESPONSE GUIDELINES:\n"
        "- Deliver direct, fluent, professional, and well-structured answers (use bold text, bullet points, or tables where appropriate).\n"
        "- Do NOT use robotic meta-commentary or filler introductions (e.g. avoid 'According to [Source 1]...', 'Based on the provided documents...', 'This information is derived from...'). State the facts authoritatively and naturally.\n"
        "- For organization-specific inquiries, ground details accurately in the provided [Context Sources].\n"
        "- When the user asks for broader definitions, national/statutory laws (e.g., Nigerian labor standards), global industry benchmarks, or general comparisons alongside internal records, seamlessly synthesize general knowledge while maintaining a clear distinction between internal company rules and general external standards.\n"
        "- If internal organizational facts are requested but absent from the context, state concisely that internal company records do not specify those details."
    )

    @classmethod
    def format_grounded_context(cls, retrieved_chunks: List[Dict[str, Any]]) -> Tuple[str, List[Dict[str, Any]]]:
        if not retrieved_chunks:
            return "", []

        context_blocks = []
        raw_sources = []

        for idx, chunk in enumerate(retrieved_chunks, 1):
            filename = chunk.get("filename", "Unknown Document")
            content = chunk.get("content", "").strip()
            score = chunk.get("rerank_score", chunk.get("vector_score", 0.0))

            # Sanitize spreadsheet empty column headers and N/A bloat (e.g. Column_2 | Column_3 ... | Column_16379)
            content = re.sub(r"(\|\s*Column_\d+\s*){3,}", "", content)
            content = re.sub(r"(\|\s*N/A\s*){4,}", "", content)
            if len(content) > 4000:
                content = content[:4000] + "\n...[truncated large tabular content]..."

            # Skip chunks that become virtually empty after removing repetitive column bloat
            if len(content.strip()) < 30:
                continue

            context_blocks.append(f"[Document: {filename} | Excerpt {idx}]\n{content}\n")
            raw_sources.append({
                "source_id": str(idx),
                "filename": filename,
                "document_id": chunk.get("document_id"),
                "department_id": chunk.get("department_id"),
                "access_level": chunk.get("access_level"),
                "preview": content[:160] + ("..." if len(content) > 160 else ""),
                "relevance_score": round(float(score), 4),
                "source_type": chunk.get("source_type", "file")
            })

        return "\n".join(context_blocks), raw_sources

    @classmethod
    def deduplicate_sources(cls, raw_sources: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        """
        Deduplicates sources by document / filename, retaining the maximum relevance score
        and counting aggregated matching chunks.
        """
        deduped: Dict[str, Dict[str, Any]] = {}
        for s in raw_sources:
            key = s.get("document_id") or s.get("filename")
            if key not in deduped:
                deduped[key] = {
                    "source_id": s.get("source_id"),
                    "filename": s.get("filename"),
                    "document_id": s.get("document_id"),
                    "department_id": s.get("department_id"),
                    "access_level": s.get("access_level"),
                    "preview": s.get("preview"),
                    "relevance_score": s.get("relevance_score", 0.0),
                    "source_type": s.get("source_type", "file"),
                    "chunk_count": 1
                }
            else:
                if s.get("relevance_score", 0.0) > deduped[key]["relevance_score"]:
                    deduped[key]["relevance_score"] = s["relevance_score"]
                deduped[key]["chunk_count"] += 1

        # Sort deduplicated sources by highest relevance score descending
        return sorted(deduped.values(), key=lambda x: x["relevance_score"], reverse=True)

    @classmethod
    def validate_grounding(cls, answer: str, sources: List[Dict[str, Any]]) -> Tuple[bool, float]:
        if not sources:
            return False, 0.0

        refusal_phrases = [
            "the system does not contain records matching",
            "could not find any records in the knowledge base"
        ]

        lower_answer = answer.lower()
        for phrase in refusal_phrases:
            if phrase in lower_answer:
                return False, 0.0

        top_score = sources[0].get("relevance_score", 0.5) if sources else 0.5
        confidence = min(max(top_score, 0.5), 1.0)
        return True, confidence

    @classmethod
    def get_out_of_context_response(cls, query: str, org_name: str = "your organization") -> Tuple[str, List[Dict[str, Any]], bool, float]:
        response = (
            f"Based on the authorized knowledge repositories of {org_name}, "
            f"the system does not contain records matching your query: '{query}'.\n\n"
            f"Please verify your query keywords or consult your department administrator for access clearance."
        )
        return response, [], False, 0.0
