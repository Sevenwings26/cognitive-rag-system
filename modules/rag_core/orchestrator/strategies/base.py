# modules/rag_core/orchestrator/strategies/base.py
from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Tuple
from sqlalchemy.orm import Session
from modules.auth.domain.tokens import TokenData

class BaseRetrievalStrategy(ABC):
    @abstractmethod
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
        """
        Executes query strategy and returns:
        (answer, sources, is_grounded, confidence)
        """
        pass
