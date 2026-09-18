# app/routes/documents.py
import hashlib
import base64
from typing import Optional, List
from fastapi import APIRouter, UploadFile, File, Form, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session

from app.dependencies import get_db, get_current_user, get_optional_user, resolve_effective_user, get_orchestrator, require_role
from modules.auth.domain.tokens import TokenData
from modules.auth.domain.models import AccessLevel
from modules.auth.repositories.user_repository import UserRepository
from modules.governance.domain.models import DocumentStatus
from modules.governance.repositories.document_repository import DocumentRepository
from modules.governance.repositories.chat_repository import ChatRepository
from modules.governance.services.audit_logger import AuditLogger
from modules.rag_core.orchestrator.unified_orchestrator import UnifiedRAGOrchestrator
from core.storage import StorageManager
from app.schemas.document import DocumentUploadResponse, DocumentItemResponse, DocumentUpdatePayload

router = APIRouter(tags=["Document Ingestion & Management"])

@router.post("/documents/upload", status_code=status.HTTP_202_ACCEPTED, response_model=DocumentUploadResponse)
@router.post("/enterprise/documents/upload", status_code=status.HTTP_202_ACCEPTED, response_model=DocumentUploadResponse)
@router.post("/chat/upload", status_code=status.HTTP_202_ACCEPTED, response_model=DocumentUploadResponse)
async def upload_document(
    request: Request,
    file: UploadFile = File(...),
    session_id: Optional[str] = Form(None),
    access_level: AccessLevel = Form(AccessLevel.DEPARTMENT),
    target_department_id: Optional[str] = Form(None),
    current_user: TokenData = Depends(get_current_user),
    db: Session = Depends(get_db),
    orchestrator: UnifiedRAGOrchestrator = Depends(get_orchestrator)
):
    dept_id = target_department_id if (
        target_department_id and current_user.role == "SUPER_ADMIN"
    ) else current_user.department_id

    if not dept_id and access_level == AccessLevel.DEPARTMENT:
        dept = UserRepository.list_org_departments(db, current_user.org_id)
        dept_id = dept[0].id if dept else ""

    if access_level == AccessLevel.CONFIDENTIAL and current_user.role == "MEMBER":
        raise HTTPException(status_code=403, detail="Members cannot upload Confidential documents")

    # Session Binding & Auto-Provisioning
    is_chat_upload = request.url.path.endswith("/chat/upload") or bool(session_id and session_id.strip())
    session_title = None

    if is_chat_upload:
        clean_session_id = session_id.strip() if (session_id and session_id.strip()) else None
        clean_title = f"Doc: {file.filename[:25]}"
        session = ChatRepository.get_or_create_session(
            db=db,
            session_id=clean_session_id,
            org_id=current_user.org_id,
            department_id=dept_id if dept_id else None,
            user_id=current_user.user_id if current_user.user_id else None,
            title=clean_title
        )
        session_id = session.id
        session_title = session.title
    else:
        session_id = None

    content = await file.read()
    file_hash = hashlib.sha256(content).hexdigest()

    doc_record = DocumentRepository.create_document(
        db=db,
        org_id=current_user.org_id,
        department_id=dept_id or "",
        uploader_id=current_user.user_id,
        filename=file.filename,
        file_hash=file_hash,
        file_size_bytes=len(content),
        mime_type=file.content_type or "application/octet-stream",
        access_level=access_level
    )

    # Claim-Check Pattern: Staging file to local storage/shared volume instead of in-band Redis Base64
    from core.storage import StorageManager
    staged_path = StorageManager.save_staged_file(
        file_bytes=content,
        filename=file.filename,
        doc_id=doc_record.id,
        org_id=current_user.org_id
    )
    task_id = "sync-executed"

    try:
        from modules.tasks.workers.ingestion_tasks import async_ingest_document_task
        task = async_ingest_document_task.delay(
            document_id=doc_record.id,
            filename=file.filename,
            staged_path=staged_path,
            org_id=current_user.org_id,
            department_id=dept_id,
            uploader_id=current_user.user_id,
            access_level=access_level.value,
            session_id=session_id,
            mime_type=file.content_type
        )
        task_id = task.id
    except Exception:
        chunk_count = orchestrator.ingest_document(
            filename=file.filename,
            file_bytes=content,
            document_id=doc_record.id,
            org_id=current_user.org_id,
            department_id=dept_id,
            uploader_id=current_user.user_id,
            access_level=access_level.value,
            session_id=session_id,
            mime_type=file.content_type
        )
        DocumentRepository.update_document_status(db, doc_record.id, DocumentStatus.INDEXED, chunk_count=chunk_count)

    AuditLogger.log(
        db=db,
        org_id=current_user.org_id,
        user_id=current_user.user_id,
        action="DOCUMENT_UPLOAD_QUEUED",
        resource_type="DOCUMENT",
        resource_id=doc_record.id,
        details={"filename": file.filename, "task_id": task_id, "session_id": session_id}
    )

    return DocumentUploadResponse(
        status="accepted",
        message=f"Document '{file.filename}' queued for background ingestion",
        document_id=doc_record.id,
        session_id=session_id,
        session_title=session_title,
        task_id=task_id
    )

@router.get("/documents", response_model=List[DocumentItemResponse])
@router.get("/enterprise/documents", response_model=List[DocumentItemResponse])
def list_documents(
    current_user: TokenData = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    docs = DocumentRepository.list_accessible_documents(
        db=db,
        org_id=current_user.org_id,
        department_id=current_user.department_id,
        user_id=current_user.user_id,
        user_role=current_user.role
    )
    return [
        DocumentItemResponse(
            id=d.id,
            filename=d.filename,
            department_id=d.department_id,
            access_level=d.access_level.value if hasattr(d.access_level, "value") else str(d.access_level),
            status=d.status.value if hasattr(d.status, "value") else str(d.status),
            size_bytes=d.file_size_bytes,
            created_at=d.created_at.isoformat()
        )
        for d in docs
    ]

@router.delete("/documents/{document_id}")
@router.delete("/enterprise/documents/{document_id}")
def delete_document(
    document_id: str,
    current_user: TokenData = Depends(require_role(["SUPER_ADMIN", "DEPT_ADMIN"])),
    db: Session = Depends(get_db),
    orchestrator: UnifiedRAGOrchestrator = Depends(get_orchestrator)
):
    """Purges a document, associated vector points in Qdrant, relational chunks, and staged disk files."""
    doc = DocumentRepository.get_document_by_id(db, document_id, current_user.org_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    if current_user.role == "DEPT_ADMIN" and doc.department_id and doc.department_id != current_user.department_id:
        raise HTTPException(status_code=403, detail="Not authorized to delete documents outside your department")

    filename = doc.filename
    job_id = doc.job_id

    # 1. Cascade Qdrant vectors and relational chunks
    orchestrator.delete_document_vectors(document_id, current_user.org_id, db=db)

    # 2. Purge local staged files
    StorageManager.purge_staged_files_for_doc(document_id, current_user.org_id)

    # 3. Synchronize parent job document count
    if job_id:
        job = DocumentRepository.get_ingestion_job(db, job_id, current_user.org_id)
        if job and job.documents_processed_count > 0:
            job.documents_processed_count -= 1

    # 4. Purge document metadata record
    DocumentRepository.delete_document(db, document_id, current_user.org_id)

    AuditLogger.log(
        db=db,
        org_id=current_user.org_id,
        user_id=current_user.user_id,
        action="DOCUMENT_DELETED",
        resource_type="DOCUMENT",
        resource_id=document_id,
        details={"filename": filename, "job_id": job_id}
    )

    return {"status": "success", "message": f"Document '{filename}' purged successfully"}

@router.patch("/documents/{document_id}")
@router.patch("/enterprise/documents/{document_id}")
@router.put("/enterprise/documents/{document_id}")
def update_document(
    document_id: str,
    payload: DocumentUpdatePayload,
    current_user: TokenData = Depends(require_role(["SUPER_ADMIN", "DEPT_ADMIN"])),
    db: Session = Depends(get_db),
    orchestrator: UnifiedRAGOrchestrator = Depends(get_orchestrator)
):
    """Updates document Access Clearance (ACL) and Department, cascading to chunks and Qdrant points."""
    doc = DocumentRepository.get_document_by_id(db, document_id, current_user.org_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    if current_user.role == "DEPT_ADMIN" and doc.department_id and doc.department_id != current_user.department_id:
        raise HTTPException(status_code=403, detail="Not authorized to modify documents outside your department")

    dept_id = doc.department_id
    if payload.target_department_id is not None:
        dept_id = payload.target_department_id if current_user.role == "SUPER_ADMIN" else current_user.department_id

    new_acl = payload.access_level or doc.access_level

    # 1. Update EnterpriseDocument in DB
    DocumentRepository.update_document_metadata(
        db=db,
        doc_id=document_id,
        org_id=current_user.org_id,
        access_level=new_acl,
        department_id=dept_id
    )

    # 2. Cascade update to DocumentChunk rows and Qdrant vector payload
    orchestrator.update_document_metadata(
        document_id=document_id,
        org_id=current_user.org_id,
        access_level=new_acl.value if hasattr(new_acl, "value") else str(new_acl),
        department_id=dept_id or "",
        db=db
    )

    AuditLogger.log(
        db=db,
        org_id=current_user.org_id,
        user_id=current_user.user_id,
        action="DOCUMENT_UPDATED",
        resource_type="DOCUMENT",
        resource_id=document_id,
        details={"filename": doc.filename, "access_level": str(new_acl), "department_id": dept_id}
    )

    return {
        "status": "success",
        "message": f"Document '{doc.filename}' access permissions updated successfully.",
        "document_id": document_id
    }

@router.get("/documents/{document_id}/status")
def get_document_indexing_status(
    document_id: str,
    user: Optional[TokenData] = Depends(get_optional_user),
    db: Session = Depends(get_db)
):
    """Monitors real-time document indexing status for personal & workspace uploads."""
    effective_user = resolve_effective_user(user, db)
    doc = DocumentRepository.get_document_by_id(db, document_id, effective_user.org_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    return {
        "document_id": doc.id,
        "filename": doc.filename,
        "status": doc.status.value if hasattr(doc.status, "value") else str(doc.status),
        "chunk_count": doc.chunk_count,
        "error_message": doc.error_message,
        "access_level": doc.access_level.value if hasattr(doc.access_level, "value") else str(doc.access_level),
        "created_at": doc.created_at.isoformat() if doc.created_at else None
    }

@router.get("/enterprise/documents/{document_id}/status")
def get_enterprise_document_indexing_status(
    document_id: str,
    current_user: TokenData = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Monitors real-time document indexing status for enterprise administration."""
    doc = DocumentRepository.get_document_by_id(db, document_id, current_user.org_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    return {
        "document_id": doc.id,
        "filename": doc.filename,
        "status": doc.status.value if hasattr(doc.status, "value") else str(doc.status),
        "chunk_count": doc.chunk_count,
        "error_message": doc.error_message,
        "access_level": doc.access_level.value if hasattr(doc.access_level, "value") else str(doc.access_level),
        "created_at": doc.created_at.isoformat() if doc.created_at else None
    }

@router.post("/documents/{document_id}/reindex", status_code=status.HTTP_202_ACCEPTED)
@router.post("/enterprise/documents/{document_id}/reindex", status_code=status.HTTP_202_ACCEPTED)
def reindex_document(
    document_id: str,
    current_user: TokenData = Depends(require_role(["SUPER_ADMIN", "DEPT_ADMIN"])),
    db: Session = Depends(get_db),
    orchestrator: UnifiedRAGOrchestrator = Depends(get_orchestrator)
):
    """Purges existing vector points and relational chunks, resetting document for re-indexing."""
    doc = DocumentRepository.get_document_by_id(db, document_id, current_user.org_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")

    # 1. Purge existing vectors and chunks
    orchestrator.delete_document_vectors(document_id, current_user.org_id, db=db)

    # 2. Reset status to PROCESSING
    DocumentRepository.update_document_status(db, document_id, DocumentStatus.PROCESSING, chunk_count=0)

    AuditLogger.log(
        db=db,
        org_id=current_user.org_id,
        user_id=current_user.user_id,
        action="DOCUMENT_REINDEX_QUEUED",
        resource_type="DOCUMENT",
        resource_id=document_id,
        details={"filename": doc.filename}
    )

    return {
        "status": "accepted",
        "message": f"Document '{doc.filename}' queued for re-indexing and dual-write vector generation.",
        "document_id": document_id
    }

