from abc import ABC, abstractmethod
from typing import List, Dict, Any, Optional, Tuple, Iterator
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
        """
        Default streaming execution generator.
        Yields ('status', dict), ('delta', dict), ('result', tuple).
        """
        res = self.execute(
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
        answer, sources, is_grounded, confidence = res
        yield ("delta", {"content": answer})
        yield ("result", res)

