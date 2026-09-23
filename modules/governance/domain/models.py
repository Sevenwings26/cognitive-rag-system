# modules/governance/domain/models.py
import enum
import uuid
from datetime import datetime
from sqlalchemy import Column, String, Text, ForeignKey, DateTime, Boolean, Enum, Integer, JSON, Index
from sqlalchemy.orm import relationship
from pgvector.sqlalchemy import Vector
from core.database import Base
from modules.auth.domain.models import AccessLevel
from core.config import settings

class DocumentStatus(str, enum.Enum):
    PENDING = "PENDING"
    PROCESSING = "PROCESSING"
    INDEXED = "INDEXED"
    FAILED = "FAILED"
    ARCHIVED = "ARCHIVED"

class IngestionJobStatus(str, enum.Enum):
    PENDING = "PENDING"
    RUNNING = "RUNNING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"

class AssistantPersona(Base):
    __tablename__ = "assistant_personas"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id = Column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    system_instruction_template = Column(Text, nullable=False)
    temperature = Column(Integer, default=2)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class PromptTemplate(Base):
    __tablename__ = "prompt_templates"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id = Column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    name = Column(String(255), nullable=False)
    description = Column(Text, nullable=True)
    user_prompt_template = Column(Text, nullable=False)
    is_active = Column(Boolean, default=True)
    created_at = Column(DateTime, default=datetime.utcnow)

class AuditLog(Base):
    __tablename__ = "audit_logs"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id = Column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    user_id = Column(String(36), nullable=True, index=True)
    action = Column(String(100), nullable=False, index=True)
    resource_type = Column(String(100), nullable=False)
    resource_id = Column(String(100), nullable=True)
    details = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow, index=True)

class EnterpriseDocument(Base):
    __tablename__ = "enterprise_documents"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id = Column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    department_id = Column(String(36), ForeignKey("departments.id", ondelete="SET NULL"), nullable=True, index=True)
    uploader_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    filename = Column(String(255), nullable=False)
    file_hash = Column(String(64), nullable=False, index=True)
    file_size_bytes = Column(Integer, default=0)
    mime_type = Column(String(100), default="application/octet-stream")
    access_level = Column(Enum(AccessLevel), default=AccessLevel.DEPARTMENT, nullable=False)
    status = Column(Enum(DocumentStatus), default=DocumentStatus.PENDING, nullable=False)
    error_message = Column(Text, nullable=True)
    chunk_count = Column(Integer, default=0)
    job_id = Column(String(36), ForeignKey("ingestion_jobs.id", ondelete="SET NULL"), nullable=True, index=True)
    external_id = Column(String(255), nullable=True, index=True)
    external_last_modified = Column(DateTime, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    chunks = relationship("DocumentChunk", back_populates="document", cascade="all, delete-orphan")

class DocumentChunk(Base):
    """
    Relational storage for individual text chunks and their embedding vectors (pgvector).
    Provides dual-persistence, relational search, and disaster recovery for Qdrant.
    """
    __tablename__ = "document_chunks"

    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    document_id = Column(String(36), ForeignKey("enterprise_documents.id", ondelete="CASCADE"), nullable=False, index=True)
    org_id = Column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    department_id = Column(String(36), nullable=True, index=True)
    uploader_id = Column(String(36), nullable=True)
    access_level = Column(String(50), nullable=False, default="DEPARTMENT")
    chunk_index = Column(Integer, nullable=False)
    content = Column(Text, nullable=False)
    embedding = Column(Vector(settings.EMBEDDING_DIMENSION), nullable=True)
    metadata_json = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    document = relationship("EnterpriseDocument", back_populates="chunks")

    __table_args__ = (
        Index("ix_doc_chunks_org_dept", "org_id", "department_id"),
        Index("ix_doc_chunks_doc_idx", "document_id", "chunk_index"),
    )


class IngestionJob(Base):
    __tablename__ = "ingestion_jobs"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id = Column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    department_id = Column(String(36), ForeignKey("departments.id", ondelete="SET NULL"), nullable=True, index=True)
    created_by_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True)
    name = Column(String(255), nullable=False)
    source_type = Column(String(50), nullable=False)
    access_level = Column(Enum(AccessLevel), default=AccessLevel.DEPARTMENT, nullable=False)
    connection_config = Column(JSON, nullable=False)
    cron_schedule = Column(String(100), nullable=True)
    status = Column(Enum(IngestionJobStatus), default=IngestionJobStatus.PENDING, nullable=False)
    last_run_at = Column(DateTime, nullable=True)
    documents_processed_count = Column(Integer, default=0)
    last_sync_token = Column(Text, nullable=True)
    error_message = Column(Text, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

# class ChatSession(Base):
#     __tablename__ = "chat_sessions"
#     id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
#     org_id = Column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
#     department_id = Column(String(36), nullable=True)
#     user_id = Column(String(36), ForeignKey("users.id", ondelete="CASCADE"), nullable=False, index=True)
#     title = Column(String(255), default="New Chat")
#     created_at = Column(DateTime, default=datetime.utcnow)
#     messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan", order_by="ChatMessage.created_at")

class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    org_id = Column(String(36), ForeignKey("organizations.id", ondelete="CASCADE"), nullable=False, index=True)
    department_id = Column(String(36), ForeignKey("departments.id", ondelete="SET NULL"), nullable=True, index=True)
    user_id = Column(String(36), ForeignKey("users.id", ondelete="SET NULL"), nullable=True, index=True)  # Nullable for guests
    title = Column(String(255), default="New Chat")
    created_at = Column(DateTime, default=datetime.utcnow)
    working_memory = Column(JSON, nullable=True)  # Durable blackboard snapshot - fallback when Redis TTL expires
    messages = relationship("ChatMessage", back_populates="session", cascade="all, delete-orphan", order_by="ChatMessage.created_at")

class ChatMessage(Base):
    __tablename__ = "chat_messages"
    id = Column(String(36), primary_key=True, default=lambda: str(uuid.uuid4()))
    session_id = Column(String(36), ForeignKey("chat_sessions.id", ondelete="CASCADE"), nullable=False, index=True)
    role = Column(String(50), nullable=False)
    content = Column(Text, nullable=False)
    citation_metadata = Column(JSON, nullable=True)
    created_at = Column(DateTime, default=datetime.utcnow)

    session = relationship("ChatSession", back_populates="messages")
