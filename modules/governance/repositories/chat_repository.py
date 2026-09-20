# modules/governance/repositories/chat_repository.py
import uuid
from typing import List, Optional, Dict, Any
from sqlalchemy.orm import Session
from modules.governance.domain.models import ChatSession, ChatMessage

class ChatRepository:
    @staticmethod
    def get_or_create_session(
        db: Session,
        session_id: Optional[str],
        org_id: str,
        department_id: Optional[str] = None,
        user_id: Optional[str] = None,
        title: str = "New Conversation"
    ) -> ChatSession:
        if session_id:
            query = db.query(ChatSession).filter(
                ChatSession.id == session_id,
                ChatSession.org_id == org_id
            )
            if user_id:
                query = query.filter(ChatSession.user_id == user_id)
            session = query.first()
            if session:
                return session

        new_session = ChatSession(
            id=session_id or str(uuid.uuid4()),
            org_id=org_id,
            department_id=department_id if department_id else None,
            user_id=user_id if user_id else None,
            title=title
        )
        db.add(new_session)
        db.commit()
        db.refresh(new_session)
        return new_session

    @staticmethod
    def list_user_sessions(db: Session, org_id: str, user_id: Optional[str] = None) -> List[ChatSession]:
        """
        Returns sessions belonging strictly to the specified authenticated user.
        Unauthenticated/guest users (user_id=None) do not receive shared cross-session lists.
        """
        if not user_id:
            return []
        return db.query(ChatSession).filter(
            ChatSession.org_id == org_id,
            ChatSession.user_id == user_id
        ).order_by(ChatSession.created_at.desc()).all()

    @staticmethod
    def get_session_by_id(db: Session, session_id: str, org_id: str, user_id: Optional[str] = None) -> Optional[ChatSession]:
        query = db.query(ChatSession).filter(
            ChatSession.id == session_id,
            ChatSession.org_id == org_id
        )
        if user_id:
            query = query.filter(ChatSession.user_id == user_id)
        return query.first()

    @staticmethod
    def add_message(
        db: Session,
        session_id: str,
        role: str,
        content: str,
        citation_metadata: Optional[Dict[str, Any]] = None
    ) -> ChatMessage:
        safe_metadata = None
        if citation_metadata is not None:
            try:
                import json
                safe_metadata = json.loads(json.dumps(citation_metadata, default=str))
            except Exception:
                safe_metadata = {}

        msg = ChatMessage(
            session_id=session_id,
            role=role,
            content=content,
            citation_metadata=safe_metadata
        )
        db.add(msg)
        db.commit()
        db.refresh(msg)
        return msg

    @staticmethod
    def get_session_messages(db: Session, session_id: str) -> List[ChatMessage]:
        return db.query(ChatMessage).filter(
            ChatMessage.session_id == session_id
        ).order_by(ChatMessage.created_at.asc()).all()

    @staticmethod
    def delete_session(db: Session, session_id: str, org_id: str) -> bool:
        session = db.query(ChatSession).filter(
            ChatSession.id == session_id,
            ChatSession.org_id == org_id
        ).first()
        if session:
            db.delete(session)
            db.commit()
            return True
        return False
