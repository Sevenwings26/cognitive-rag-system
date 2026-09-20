# modules/rag_core/orchestrator/strategies/conversational_strategy.py
import logging
from typing import List, Dict, Any, Optional, Tuple, Iterator
from sqlalchemy.orm import Session

from modules.auth.domain.tokens import TokenData
from modules.governance.domain.models import AssistantPersona
from modules.governance.services.audit_logger import AuditLogger
from modules.rag_core.providers.llm import BaseLLMService
from modules.rag_core.guardrails.prompt_engine import PromptEngine
from modules.rag_core.orchestrator.strategies.base import BaseRetrievalStrategy

logger = logging.getLogger("conversational_strategy")

class ConversationalStrategy(BaseRetrievalStrategy):
    """
    Route A: Zero-Retrieval Conversational Strategy.
    Bypasses vector search completely for greetings, general questions, and personality/persona turns.
    """
    def __init__(self, llm_service: BaseLLMService, prompt_engine: Optional[PromptEngine] = None):
        self.llm = llm_service
        self.prompt_engine = prompt_engine or PromptEngine()

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
        org_name = kwargs.get("org_name", "Enterprise")
        dept_name = kwargs.get("dept_name", "General")
        intent_category = kwargs.get("intent_category", "CONVERSATIONAL")

        logger.info(f"[CONVERSATIONAL STRATEGY] Executing for user={user_context.email}, intent={intent_category}")

        template_vars = {
            "user_name": user_context.email.split("@")[0] if user_context.email else "User",
            "user_role": user_context.role,
            "department_name": dept_name,
            "org_name": org_name,
            "query": query
        }

        system_instruction = (
            f"You are a helpful and knowledgeable AI assistant for {org_name}. "
            "Answer the user's question clearly, politely, and accurately using your general knowledge."
        )
        temperature = 0.5

        if persona_id and db:
            persona = db.query(AssistantPersona).filter(
                AssistantPersona.id == persona_id,
                AssistantPersona.org_id == user_context.org_id,
                AssistantPersona.is_active == True
            ).first()
            if persona:
                system_instruction = self.prompt_engine.render_template(persona.system_instruction_template, template_vars)
                temperature = persona.temperature / 10.0 if persona.temperature > 1 else persona.temperature

        yield ("status", {
            "step": "conversational_synthesis",
            "stage": "synthesis",
            "title": "Generating Conversational Response",
            "details": f"Generating assistant response tailored for {org_name}...",
            "status": "in_progress"
        })

        answer = ""
        if hasattr(self.llm, "stream_text"):
            for token in self.llm.stream_text(
                query,
                system_instruction=system_instruction,
                temperature=max(temperature, 0.5)
            ):
                answer += token
                yield ("delta", {"content": token})
        else:
            answer = self.llm.generate_text(
                query,
                system_instruction=system_instruction,
                temperature=max(temperature, 0.5)
            )
            yield ("delta", {"content": answer})

        if db:
            AuditLogger.log(
                db=db,
                org_id=user_context.org_id,
                user_id=user_context.user_id,
                action="GENERAL_CHAT_QUERY",
                resource_type="CHAT_QUERY",
                resource_id=session_id,
                details={"query": query[:200], "intent": intent_category}
            )

        yield ("status", {
            "step": "conversational_synthesis",
            "stage": "synthesis",
            "title": "Response Generated",
            "details": "Conversational response generated.",
            "status": "completed"
        })

        yield ("result", (answer, [], True, 1.0))

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

