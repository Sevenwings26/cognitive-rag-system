"""
On-Premise PostgreSQL Checkpointer for LangGraph Multi-Tenant Orchestrator.

Replaces ephemeral/cloud Redis with transactional PostgreSQL checkpoints,
isolating tenant sessions using composite thread identifiers.
"""

import logging
from typing import Optional, Any
from core.config import settings

logger = logging.getLogger(__name__)


def get_postgres_checkpointer() -> Any:
    """
    Initializes and returns the on-premise PostgreSQL checkpointer.
    
    In multi-tenant environments, graph invocations supply:
    config = {"configurable": {"thread_id": f"tenant_{org_id}:session_{session_id}"}}
    """
    try:
        from langgraph.checkpoint.postgres import PostgresSaver
        import psycopg

        db_url = settings.DATABASE_URL
        # Normalize driver for psycopg3 if necessary
        if "postgresql+psycopg://" in db_url:
            db_url = db_url.replace("postgresql+psycopg://", "postgresql://")
        elif "postgresql+psycopg2://" in db_url:
            db_url = db_url.replace("postgresql+psycopg2://", "postgresql://")

        conn = psycopg.connect(db_url, autocommit=True)
        checkpointer = PostgresSaver(conn)
        checkpointer.setup()
        logger.info("[CHECKPOINTER] Successfully connected to on-premise PostgreSQL checkpointer.")
        return checkpointer
    except ImportError:
        logger.warning(
            "[CHECKPOINTER] langgraph.checkpoint.postgres not found. "
            "Falling back to in-memory session checkpointer for on-premise execution."
        )
        try:
            from langgraph.checkpoint.memory import MemorySaver
            return MemorySaver()
        except ImportError:
            return None
    except Exception as e:
        logger.error(f"[CHECKPOINTER] Failed to initialize PostgreSQL checkpointer: {e}")
        return None
