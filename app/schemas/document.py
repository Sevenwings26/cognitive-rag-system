# app/schemas/document.py
from typing import Optional, Dict, Any
from pydantic import BaseModel
from modules.auth.domain.models import AccessLevel
from modules.governance.domain.models import DocumentStatus

class DocumentUploadResponse(BaseModel):
    status: str
    message: str
    document_id: Optional[str] = None
    session_id: Optional[str] = None
    task_id: Optional[str] = None

class DocumentItemResponse(BaseModel):
    id: str
    filename: str
    department_id: Optional[str] = None
    access_level: str
    status: str
    size_bytes: int
    created_at: str

class IngestionJobCreatePayload(BaseModel):
    name: str
    source_type: str
    access_level: AccessLevel = AccessLevel.DEPARTMENT
    target_department_id: Optional[str] = None
    connection_config: Dict[str, Any]
    cron_schedule: Optional[str] = None

class IngestionJobResponse(BaseModel):
    id: str
    name: str
    source_type: str
    access_level: str
    status: str
    last_run_at: Optional[str] = None
    documents_processed_count: int
    created_at: str

class IngestionJobUpdatePayload(BaseModel):
    name: Optional[str] = None
    access_level: Optional[AccessLevel] = None
    target_department_id: Optional[str] = None
    connection_config: Optional[Dict[str, Any]] = None
    cron_schedule: Optional[str] = None

class IngestionJobDetailResponse(BaseModel):
    id: str
    name: str
    source_type: str
    access_level: str
    department_id: Optional[str] = None
    status: str
    last_run_at: Optional[str] = None
    documents_processed_count: int
    cron_schedule: Optional[str] = None
    connection_config: Dict[str, Any]
    created_at: str

class DocumentUpdatePayload(BaseModel):
    access_level: Optional[AccessLevel] = None
    target_department_id: Optional[str] = None
