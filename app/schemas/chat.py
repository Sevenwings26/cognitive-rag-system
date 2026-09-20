# app/schemas/chat.py
from typing import Optional, List, Dict, Any, Union
from pydantic import BaseModel, Field

class ChatQueryPayload(BaseModel):
    """
    Unified query payload supporting personal, departmental, and enterprise scopes.
    """
    query: str = Field(..., description="The user's query text")
    session_id: Optional[str] = Field(None, description="Active conversational thread ID")
    scope: Optional[Union[str, List[str]]] = Field(
        default=["personal", "department", "enterprise"],
        description="Allowed search scopes: 'personal', 'department', 'enterprise'"
    )
    persona_id: Optional[str] = Field(None, description="Optional Assistant Persona ID")
    template_id: Optional[str] = Field(None, description="Optional Prompt Template ID")
    top_k: int = Field(3, description="Number of final context chunks after reranking")
    score_threshold: float = Field(0.35, description="Minimum similarity score threshold")
    mode: Optional[str] = Field("auto", description="Execution mode: 'auto', 'rag', 'general'")
    stream: bool = Field(False, description="Whether to stream retrieval milestones and response tokens via Server-Sent Events (SSE)")

class SourceCitation(BaseModel):
    filename: str
    document_id: Optional[str] = None
    department_id: Optional[str] = None
    access_level: Optional[str] = None
    preview: str
    relevance_score: float
    source_type: Optional[str] = "file"
    chunk_count: Optional[int] = 1

class ChatQueryResponse(BaseModel):
    status: str = "success"
    session_id: Optional[str] = None
    session_title: Optional[str] = None
    answer: str
    sources: List[SourceCitation] = []
    is_grounded: bool = True
    grounding_confidence: float = 1.0

class ChatSessionItem(BaseModel):
    id: str
    title: str
    created_at: str

class ChatMessageItem(BaseModel):
    id: str
    role: str
    content: str
    created_at: str
    citation_metadata: Optional[Dict[str, Any]] = None
