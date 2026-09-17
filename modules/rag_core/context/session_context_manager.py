# modules/rag_core/context/session_context_manager.py
import logging
from typing import Dict, Any, List, Optional
from sqlalchemy.orm import Session

from core.config import settings
from modules.rag_core.context.models import SessionWorkingMemory
from modules.governance.repositories.chat_repository import ChatRepository

logger = logging.getLogger("session_context_manager")

# Process-level in-memory fallback cache
_IN_MEMORY_CACHE: Dict[str, SessionWorkingMemory] = {}

class SessionContextManager:
    """
    Universal Session Context & Working Memory Manager.
    Persists multi-turn entity bindings, metrics, and document references
    across requests using Redis (with in-memory dictionary fallback).
    """

    _redis_client = None
    _redis_initialized = False

    @classmethod
    def _get_redis(cls):
        if not cls._redis_initialized:
            cls._redis_initialized = True
            try:
                import redis
                redis_url = getattr(settings, "REDIS_URL", "redis://redis:6379/0")
                client = redis.Redis.from_url(redis_url, decode_responses=True, socket_connect_timeout=2)
                client.ping()
                cls._redis_client = client
                logger.info(f"[SESSION CONTEXT] Connected to Redis at {redis_url}")
            except Exception as e:
                logger.warning(f"[SESSION CONTEXT] Redis unavailable ({e}). Operating in in-memory mode.")
                cls._redis_client = None
        return cls._redis_client

    @classmethod
    def get_memory(cls, session_id: Optional[str]) -> SessionWorkingMemory:
        """Retrieves or initializes the working memory for a conversation session."""
        if not session_id:
            return SessionWorkingMemory(session_id="transient")

        r = cls._get_redis()
        if r:
            try:
                raw = r.get(f"session_memory:{session_id}")
                if raw:
                    return SessionWorkingMemory.from_json(raw)
            except Exception as e:
                logger.warning(f"[SESSION CONTEXT] Redis read error: {e}")

        # Fallback to local memory cache
        if session_id in _IN_MEMORY_CACHE:
            return _IN_MEMORY_CACHE[session_id]

        new_mem = SessionWorkingMemory(session_id=session_id)
        _IN_MEMORY_CACHE[session_id] = new_mem
        return new_mem

    @classmethod
    def save_memory(cls, memory: SessionWorkingMemory, ttl_seconds: int = 86400) -> None:
        """Persists updated working memory to Redis and in-memory cache."""
        if not memory.session_id or memory.session_id == "transient":
            return

        # Always update local cache
        _IN_MEMORY_CACHE[memory.session_id] = memory

        r = cls._get_redis()
        if r:
            try:
                r.set(f"session_memory:{memory.session_id}", memory.to_json(), ex=ttl_seconds)
            except Exception as e:
                logger.warning(f"[SESSION CONTEXT] Redis write error: {e}")

    @classmethod
    def clear_memory(cls, session_id: str) -> None:
        """Purges working memory for a given session."""
        _IN_MEMORY_CACHE.pop(session_id, None)
        r = cls._get_redis()
        if r:
            try:
                r.delete(f"session_memory:{session_id}")
            except Exception as e:
                logger.warning(f"[SESSION CONTEXT] Redis delete error: {e}")

    @classmethod
    def get_recent_history(
        cls,
        db: Optional[Session],
        session_id: Optional[str],
        current_query: Optional[str] = None,
        max_messages: int = 6
    ) -> List[Dict[str, str]]:
        """
        Retrieves recent conversation turns from the database for multi-turn context.
        Excludes the current user query if it was already inserted into the database.
        """
        if not db or not session_id:
            return []

        try:
            db_messages = ChatRepository.get_session_messages(db, session_id)
            if not db_messages:
                return []

            # If the last message in DB is the current user query (added right before orchestrator call),
            # exclude it from the history window so it is not duplicated.
            history_candidates = list(db_messages)
            if history_candidates and current_query:
                last_msg = history_candidates[-1]
                if last_msg.role == "user" and last_msg.content.strip() == current_query.strip():
                    history_candidates = history_candidates[:-1]

            # Take the last max_messages
            recent = history_candidates[-max_messages:]
            return [{"role": m.role, "content": m.content} for m in recent]
        except Exception as e:
            logger.warning(f"[SESSION CONTEXT] Failed to retrieve history: {e}")
            return []
