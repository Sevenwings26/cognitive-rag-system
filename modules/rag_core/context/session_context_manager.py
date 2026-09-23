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

    Three-tier persistence strategy:
      1. Redis (hot)         — primary store, 24h TTL, sub-millisecond reads
      2. PostgreSQL (warm)   — durable fallback written on every save_memory();
                               loaded when Redis key has expired (e.g. user returns after 24h+)
      3. In-memory dict      — process-local last resort when Redis is unavailable

    This ensures that resolved entity bindings (customer_id, BVN, balances, etc.)
    survive across long session gaps without requiring the user to re-identify
    themselves in a new conversation turn.
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
    def get_memory(cls, session_id: Optional[str], db: Optional[Session] = None) -> SessionWorkingMemory:
        """
        Retrieves or initialises the working memory for a conversation session.

        Resolution order:
          1. Redis  — fast path, sub-ms
          2. PostgreSQL chat_sessions.working_memory  — warm path (Redis expired)
          3. In-memory cache  — Redis-down path
          4. Fresh empty SessionWorkingMemory  — first visit
        """
        if not session_id:
            return SessionWorkingMemory(session_id="transient")

        # --- Tier 1: Redis (hot path) ---
        r = cls._get_redis()
        if r:
            try:
                raw = r.get(f"session_memory:{session_id}")
                if raw:
                    memory = SessionWorkingMemory.from_json(raw)
                    _IN_MEMORY_CACHE[session_id] = memory  # Keep local cache in sync
                    return memory
            except Exception as e:
                logger.warning(f"[SESSION CONTEXT] Redis read error: {e}")

        # --- Tier 2: PostgreSQL (warm path — Redis TTL expired) ---
        if db:
            try:
                from modules.governance.domain.models import ChatSession
                session_row = db.query(ChatSession).filter(
                    ChatSession.id == session_id
                ).first()
                if session_row and session_row.working_memory:
                    memory = SessionWorkingMemory.from_dict(session_row.working_memory)
                    # Repopulate Redis so the next read hits the fast path again
                    cls._write_to_redis(r, memory)
                    _IN_MEMORY_CACHE[session_id] = memory
                    logger.info(
                        f"[SESSION CONTEXT] Blackboard restored from PostgreSQL for session '{session_id}' "
                        f"(turn {memory.turn_count}, scope={memory.scope})"
                    )
                    return memory
            except Exception as e:
                logger.warning(f"[SESSION CONTEXT] PostgreSQL blackboard read error: {e}")

        # --- Tier 3: In-memory fallback (Redis unavailable) ---
        if session_id in _IN_MEMORY_CACHE:
            return _IN_MEMORY_CACHE[session_id]

        # --- Tier 4: Fresh slate ---
        new_mem = SessionWorkingMemory(session_id=session_id)
        _IN_MEMORY_CACHE[session_id] = new_mem
        return new_mem

    @classmethod
    def save_memory(cls, memory: SessionWorkingMemory, ttl_seconds: int = 86400, db: Optional[Session] = None) -> None:
        """
        Persists updated working memory to all available tiers.

          - Redis: always attempted first (hot cache, 24h TTL)
          - PostgreSQL: written on every call so the blackboard is always durable
          - In-memory: always updated as a local fallback

        The PostgreSQL write is the key addition: it means even if the Redis key
        expires after 24 hours, get_memory() will warm the cache back from PG.
        """
        if not memory.session_id or memory.session_id == "transient":
            return

        # Always update local cache
        _IN_MEMORY_CACHE[memory.session_id] = memory

        # Tier 1: Redis
        r = cls._get_redis()
        cls._write_to_redis(r, memory, ttl_seconds)

        # Tier 2: PostgreSQL — durable snapshot
        if db:
            try:
                from modules.governance.domain.models import ChatSession
                session_row = db.query(ChatSession).filter(
                    ChatSession.id == memory.session_id
                ).first()
                if session_row:
                    session_row.working_memory = memory.to_dict()
                    db.commit()
            except Exception as e:
                logger.warning(f"[SESSION CONTEXT] PostgreSQL blackboard write error: {e}")

    @classmethod
    def _write_to_redis(cls, r, memory: SessionWorkingMemory, ttl_seconds: int = 86400) -> None:
        """Internal helper: write memory to Redis with TTL."""
        if r:
            try:
                r.set(f"session_memory:{memory.session_id}", memory.to_json(), ex=ttl_seconds)
            except Exception as e:
                logger.warning(f"[SESSION CONTEXT] Redis write error: {e}")

    @classmethod
    def clear_memory(cls, session_id: str, db: Optional[Session] = None) -> None:
        """Purges working memory from all tiers for a given session."""
        _IN_MEMORY_CACHE.pop(session_id, None)

        r = cls._get_redis()
        if r:
            try:
                r.delete(f"session_memory:{session_id}")
            except Exception as e:
                logger.warning(f"[SESSION CONTEXT] Redis delete error: {e}")

        if db:
            try:
                from modules.governance.domain.models import ChatSession
                session_row = db.query(ChatSession).filter(
                    ChatSession.id == session_id
                ).first()
                if session_row:
                    session_row.working_memory = None
                    db.commit()
            except Exception as e:
                logger.warning(f"[SESSION CONTEXT] PostgreSQL blackboard clear error: {e}")

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
