from typing import List, Dict, Any, Optional
from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session

from app.dependencies import get_db, get_current_user, require_role, get_orchestrator
from modules.auth.domain.tokens import TokenData
from modules.auth.domain.models import AccessLevel
from modules.governance.domain.models import IngestionJobStatus
from modules.governance.repositories.document_repository import DocumentRepository
from modules.governance.services.audit_logger import AuditLogger
from modules.connectors.registry import ConnectorRegistry
from modules.connectors.schemas import (
    ConnectorDescriptor, ConnectorTestRequest, ConnectorTestResponse
)
from modules.connectors.security.sql_guard import (
    InsecureQueryError, DatabaseSecurityTargetError, SQLSecurityGuard
)
from modules.rag_core.orchestrator.unified_orchestrator import UnifiedRAGOrchestrator
from core.crypto import encrypt_connection_config, decrypt_connection_config
from core.storage import StorageManager
from app.schemas.document import (
    IngestionJobCreatePayload, IngestionJobResponse, IngestionJobUpdatePayload, IngestionJobDetailResponse
)

router = APIRouter(prefix="/enterprise", tags=["Enterprise Knowledge Connectors & Jobs"])

@router.get("/connectors/schemas", response_model=List[ConnectorDescriptor])
def list_connector_schemas(
    current_user: TokenData = Depends(get_current_user)
):
    """
    Returns frontend-ready typed schemas and visual metadata for all 
    supported enterprise knowledge connectors (Databases, Cloud Storage, Wikis).
    """
    return ConnectorRegistry.get_descriptor_list()

@router.post("/connectors/test-connection", response_model=ConnectorTestResponse)
def test_connector_connection(
    payload: ConnectorTestRequest,
    current_user: TokenData = Depends(require_role(["SUPER_ADMIN", "DEPT_ADMIN"])),
    db: Session = Depends(get_db)
):
    """
    Executes a fast pre-flight connection handshake, credential validation,
    SSRF checking, AST query validation, and latency measurement for an enterprise data source.
    """
    try:
        connector = ConnectorRegistry.get_connector(payload.source_type, payload.connection_config)
        test_result = connector.test_connection()

        if not test_result.get("success", False) and "Security Violation" in test_result.get("message", ""):
            AuditLogger.log(
                db=db,
                org_id=current_user.org_id,
                user_id=current_user.user_id,
                action="SECURITY_QUERY_BLOCKED",
                resource_type="CONNECTOR_TEST",
                resource_id=payload.source_type,
                details={"source_type": payload.source_type, "message": test_result.get("message")}
            )

        return ConnectorTestResponse(
            success=test_result.get("success", False),
            latency_ms=test_result.get("latency_ms", 0.0),
            message=test_result.get("message", "Connection test completed."),
            details=test_result.get("details")
        )
    except InsecureQueryError as e:
        AuditLogger.log(
            db=db,
            org_id=current_user.org_id,
            user_id=current_user.user_id,
            action="SECURITY_QUERY_BLOCKED",
            resource_type="CONNECTOR_TEST",
            resource_id=payload.source_type,
            details={"source_type": payload.source_type, "violation": str(e)}
        )
        return ConnectorTestResponse(
            success=False,
            latency_ms=0.0,
            message=f"Security Violation: {str(e)}",
            details=None
        )
    except DatabaseSecurityTargetError as e:
        return ConnectorTestResponse(
            success=False,
            latency_ms=0.0,
            message=f"Network Security Block: {str(e)}",
            details=None
        )
    except Exception as e:
        return ConnectorTestResponse(
            success=False,
            latency_ms=0.0,
            message=f"Connector Initialization Error: {str(e)}",
            details=None
        )

@router.get("/jobs", response_model=List[IngestionJobResponse])
def list_jobs(
    current_user: TokenData = Depends(get_current_user),
    db: Session = Depends(get_db)
):
    """Lists all configured data source ingestion jobs for the tenant."""
    jobs = DocumentRepository.list_ingestion_jobs(db, current_user.org_id)
    return [
        IngestionJobResponse(
            id=j.id,
            name=j.name,
            source_type=j.source_type,
            access_level=j.access_level.value if hasattr(j.access_level, "value") else str(j.access_level),
            status=j.status.value if hasattr(j.status, "value") else str(j.status),
            last_run_at=j.last_run_at.isoformat() if j.last_run_at else None,
            documents_processed_count=j.documents_processed_count,
            created_at=j.created_at.isoformat()
        )
        for j in jobs
    ]

@router.post("/jobs/create")
def create_job(
    payload: IngestionJobCreatePayload,
    current_user: TokenData = Depends(require_role(["SUPER_ADMIN", "DEPT_ADMIN"])),
    db: Session = Depends(get_db)
):
    """Creates a new scheduled/manual connector sync job with encrypted connection config and AST validation."""
    dept_id = payload.target_department_id if (
        payload.target_department_id and current_user.role == "SUPER_ADMIN"
    ) else current_user.department_id

    if not dept_id and payload.access_level != "PUBLIC":
        raise HTTPException(status_code=400, detail="Department ID is required for department-scoped sync jobs")

    # 1. Validate connector configuration & AST query guard
    try:
        ConnectorRegistry.get_connector(payload.source_type, payload.connection_config)
    except InsecureQueryError as e:
        AuditLogger.log(
            db=db,
            org_id=current_user.org_id,
            user_id=current_user.user_id,
            action="SECURITY_QUERY_BLOCKED",
            resource_type="INGESTION_JOB_CREATE",
            resource_id=payload.name,
            details={"source_type": payload.source_type, "violation": str(e)}
        )
        raise HTTPException(status_code=400, detail=f"Security Policy Violation: {str(e)}")
    except DatabaseSecurityTargetError as e:
        raise HTTPException(status_code=400, detail=f"Network Security Policy Block: {str(e)}")
    except Exception as e:
        raise HTTPException(status_code=400, detail=f"Invalid connector configuration: {str(e)}")

    # 2. Encrypt sensitive connection config at rest
    encrypted_config = encrypt_connection_config(payload.connection_config)

    job = DocumentRepository.create_ingestion_job(
        db=db,
        org_id=current_user.org_id,
        department_id=dept_id,
        created_by_id=current_user.user_id,
        name=payload.name,
        source_type=payload.source_type,
        access_level=payload.access_level,
        connection_config=encrypted_config,
        cron_schedule=payload.cron_schedule
    )

    AuditLogger.log(
        db=db,
        org_id=current_user.org_id,
        user_id=current_user.user_id,
        action="INGESTION_JOB_CREATED",
        resource_type="INGESTION_JOB",
        resource_id=job.id,
        details={"name": job.name, "source_type": job.source_type, "access_level": str(job.access_level)}
    )

    return {"status": "success", "job_id": job.id, "name": job.name}

@router.post("/jobs/{job_id}/trigger", status_code=status.HTTP_202_ACCEPTED)
def trigger_job(
    job_id: str,
    current_user: TokenData = Depends(require_role(["SUPER_ADMIN", "DEPT_ADMIN"])),
    db: Session = Depends(get_db)
):
    """Triggers an asynchronous background Celery sync task for the connector job."""
    job = DocumentRepository.get_ingestion_job(db, job_id, current_user.org_id)
    if not job:
        raise HTTPException(status_code=404, detail="Ingestion job not found")

    task_id = "sync-executed"
    try:
        from modules.tasks.workers.ingestion_tasks import async_execute_ingestion_job_task
        task = async_execute_ingestion_job_task.delay(job_id)
        task_id = task.id
    except Exception:
        from modules.tasks.workers.ingestion_tasks import async_execute_ingestion_job_task
        async_execute_ingestion_job_task(job_id)

    AuditLogger.log(
        db=db,
        org_id=current_user.org_id,
        user_id=current_user.user_id,
        action="INGESTION_JOB_TRIGGERED",
        resource_type="INGESTION_JOB",
        resource_id=job.id,
        details={"task_id": task_id, "job_name": job.name}
    )

    return {"status": "accepted", "message": f"Job '{job.name}' queued for processing", "task_id": task_id}
@router.get("/jobs/{job_id}", response_model=IngestionJobDetailResponse)
def get_job(
    job_id: str,
    current_user: TokenData = Depends(require_role(["SUPER_ADMIN", "DEPT_ADMIN"])),
    db: Session = Depends(get_db)
):
    """Retrieves single connector job details with decrypted connection configuration for administrative editing."""
    job = DocumentRepository.get_ingestion_job(db, job_id, current_user.org_id)
    if not job:
        raise HTTPException(status_code=404, detail="Ingestion job not found")

    if current_user.role == "DEPT_ADMIN" and job.department_id and job.department_id != current_user.department_id:
        raise HTTPException(status_code=403, detail="Not authorized to access another department's connector job")

    try:
        decrypted_config = decrypt_connection_config(job.connection_config)
    except Exception:
        decrypted_config = job.connection_config if isinstance(job.connection_config, dict) else {}

    return IngestionJobDetailResponse(
        id=job.id,
        name=job.name,
        source_type=job.source_type,
        access_level=job.access_level.value if hasattr(job.access_level, "value") else str(job.access_level),
        department_id=job.department_id,
        status=job.status.value if hasattr(job.status, "value") else str(job.status),
        last_run_at=job.last_run_at.isoformat() if job.last_run_at else None,
        documents_processed_count=job.documents_processed_count,
        cron_schedule=job.cron_schedule,
        connection_config=decrypted_config,
        created_at=job.created_at.isoformat()
    )

@router.put("/jobs/{job_id}")
@router.patch("/jobs/{job_id}")
def update_job(
    job_id: str,
    payload: IngestionJobUpdatePayload,
    current_user: TokenData = Depends(require_role(["SUPER_ADMIN", "DEPT_ADMIN"])),
    db: Session = Depends(get_db),
    orchestrator: UnifiedRAGOrchestrator = Depends(get_orchestrator)
):
    """Updates an existing connector job configuration, merges credentials, and cascades ACL updates."""
    job = DocumentRepository.get_ingestion_job(db, job_id, current_user.org_id)
    if not job:
        raise HTTPException(status_code=404, detail="Ingestion job not found")

    if current_user.role == "DEPT_ADMIN" and job.department_id and job.department_id != current_user.department_id:
        raise HTTPException(status_code=403, detail="Not authorized to modify another department's connector job")

    dept_id = job.department_id
    if payload.target_department_id is not None:
        dept_id = payload.target_department_id if current_user.role == "SUPER_ADMIN" else current_user.department_id

    encrypted_config = None
    if payload.connection_config is not None:
        # Decrypt existing config to preserve unchanged credentials
        try:
            current_cfg = decrypt_connection_config(job.connection_config)
        except Exception:
            current_cfg = {}

        merged_cfg = dict(current_cfg)
        for k, v in payload.connection_config.items():
            # If value is non-empty, update it. If string is empty or masked, retain current value if present
            if v is not None and v != "" and v != "******":
                merged_cfg[k] = v
            elif k not in merged_cfg:
                merged_cfg[k] = v

        # Validate with ConnectorRegistry & SQL Security Guard
        try:
            ConnectorRegistry.get_connector(job.source_type, merged_cfg)
        except InsecureQueryError as e:
            AuditLogger.log(
                db=db,
                org_id=current_user.org_id,
                user_id=current_user.user_id,
                action="SECURITY_QUERY_BLOCKED",
                resource_type="INGESTION_JOB_UPDATE",
                resource_id=job.id,
                details={"source_type": job.source_type, "violation": str(e)}
            )
            raise HTTPException(status_code=400, detail=f"Security Policy Violation: {str(e)}")
        except DatabaseSecurityTargetError as e:
            raise HTTPException(status_code=400, detail=f"Network Security Policy Block: {str(e)}")
        except Exception as e:
            raise HTTPException(status_code=400, detail=f"Invalid connector configuration: {str(e)}")

        encrypted_config = encrypt_connection_config(merged_cfg)

    # If access_level or department changed, cascade to existing job documents and vector embeddings
    old_acl = job.access_level
    new_acl = payload.access_level or job.access_level
    acl_or_dept_changed = (payload.access_level is not None and payload.access_level != old_acl) or (payload.target_department_id is not None and dept_id != job.department_id)

    updated_job = DocumentRepository.update_ingestion_job(
        db=db,
        job=job,
        name=payload.name,
        access_level=payload.access_level,
        department_id=dept_id,
        connection_config=encrypted_config,
        cron_schedule=payload.cron_schedule
    )

    if acl_or_dept_changed:
        job_docs = DocumentRepository.list_job_documents(db, current_user.org_id, job.id)
        for doc in job_docs:
            DocumentRepository.update_document_metadata(
                db=db,
                doc_id=doc.id,
                org_id=current_user.org_id,
                access_level=new_acl,
                department_id=dept_id
            )
            orchestrator.update_document_metadata(
                document_id=doc.id,
                org_id=current_user.org_id,
                access_level=new_acl.value if hasattr(new_acl, "value") else str(new_acl),
                department_id=dept_id or "",
                db=db
            )

    AuditLogger.log(
        db=db,
        org_id=current_user.org_id,
        user_id=current_user.user_id,
        action="INGESTION_JOB_UPDATED",
        resource_type="INGESTION_JOB",
        resource_id=job.id,
        details={"name": updated_job.name, "source_type": updated_job.source_type, "access_level": str(updated_job.access_level)}
    )

    return {"status": "success", "job_id": updated_job.id, "name": updated_job.name, "message": "Pipeline configuration updated successfully"}

@router.delete("/jobs/{job_id}")
def delete_job(
    job_id: str,
    current_user: TokenData = Depends(require_role(["SUPER_ADMIN", "DEPT_ADMIN"])),
    db: Session = Depends(get_db),
    orchestrator: UnifiedRAGOrchestrator = Depends(get_orchestrator)
):
    """Purges a connector pipeline and cascades deletion across all associated documents, staging files, and Qdrant vectors."""
    job = DocumentRepository.get_ingestion_job(db, job_id, current_user.org_id)
    if not job:
        raise HTTPException(status_code=404, detail="Ingestion job not found")

    if current_user.role == "DEPT_ADMIN" and job.department_id and job.department_id != current_user.department_id:
        raise HTTPException(status_code=403, detail="Not authorized to delete another department's connector job")

    if job.status == IngestionJobStatus.RUNNING:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Cannot delete connector job while sync task is actively running. Please wait for completion."
        )

    job_name = job.name
    # 1. Find all documents associated with this job
    job_docs = DocumentRepository.list_job_documents(db, current_user.org_id, job.id)
    purged_doc_count = len(job_docs)

    # 2. Cascade purge for each document
    for doc in job_docs:
        # Cascade Qdrant vectors and relational chunks
        orchestrator.delete_document_vectors(doc.id, current_user.org_id, db=db)
        # Purge local staged files
        StorageManager.purge_staged_files_for_doc(doc.id, current_user.org_id)
        # Delete document record
        DocumentRepository.delete_document(db, doc.id, current_user.org_id)

    # 3. Delete the ingestion job
    DocumentRepository.delete_ingestion_job(db, job)

    AuditLogger.log(
        db=db,
        org_id=current_user.org_id,
        user_id=current_user.user_id,
        action="INGESTION_JOB_DELETED",
        resource_type="INGESTION_JOB",
        resource_id=job_id,
        details={"job_name": job_name, "purged_docs_count": purged_doc_count}
    )

    return {
        "status": "success",
        "message": f"Pipeline '{job_name}' and {purged_doc_count} associated document source(s) and vector embeddings purged successfully.",
        "purged_docs_count": purged_doc_count
    }
