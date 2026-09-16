# modules/tasks/workers/ingestion_tasks.py
import os
import hashlib
import logging
import base64
from datetime import datetime
from typing import Optional, Dict, Any, List
from celery import group
from modules.tasks.celery_app import celery_app
from core.database import SessionLocal
from core.storage import StorageManager
from modules.governance.domain.models import (
    EnterpriseDocument, DocumentStatus, IngestionJob, IngestionJobStatus
)
from modules.governance.repositories.document_repository import DocumentRepository
from modules.rag_core.providers.llm import LLMFactory
from modules.rag_core.orchestrator.unified_orchestrator import UnifiedRAGOrchestrator
from modules.connectors.registry import ConnectorRegistry
from core.crypto import decrypt_connection_config

logger = logging.getLogger("ingestion_worker")

def get_services():
    db = SessionLocal()
    llm_service = LLMFactory.get_provider()
    orchestrator = UnifiedRAGOrchestrator(llm_service=llm_service)
    return db, orchestrator

@celery_app.task(name="async_ingest_document_task")
def async_ingest_document_task(
    document_id: str,
    filename: str,
    org_id: str,
    department_id: str,
    uploader_id: str,
    access_level: str,
    staged_path: Optional[str] = None,
    file_bytes_b64: Optional[str] = None,
    session_id: Optional[str] = None,
    mime_type: Optional[str] = None
):
    """
    Asynchronously processes, chunks, embeds, and indexes a single uploaded document.
    Uses Claim-Check pattern: reads from staged_path to prevent Redis message bloat.
    """
    db, orchestrator = get_services()
    try:
        DocumentRepository.update_document_status(db, document_id, DocumentStatus.PROCESSING)

        # 1. Claim-Check: Retrieve bytes from staged disk storage or legacy base64
        if staged_path and os.path.exists(staged_path):
            file_bytes = StorageManager.read_staged_file(staged_path)
        elif file_bytes_b64:
            file_bytes = base64.b64decode(file_bytes_b64.encode("utf-8"))
        else:
            raise FileNotFoundError(f"No valid file source found for document {document_id}")

        chunk_count = orchestrator.ingest_document(
            filename=filename,
            file_bytes=file_bytes,
            document_id=document_id,
            org_id=org_id,
            department_id=department_id,
            uploader_id=uploader_id,
            access_level=access_level,
            session_id=session_id,
            mime_type=mime_type,
            db=db
        )

        DocumentRepository.update_document_status(
            db, document_id, DocumentStatus.INDEXED, chunk_count=chunk_count
        )
        logger.info(f"[ASYNC WORKER] Successfully indexed doc {document_id} ({chunk_count} chunks)")

        # Cleanup staged file upon successful indexing
        if staged_path:
            StorageManager.delete_staged_file(staged_path)

        return {"status": "success", "document_id": document_id, "chunks": chunk_count}
    except Exception as e:
        logger.error(f"[ASYNC WORKER FAILURE] Document {document_id} failed: {e}")
        DocumentRepository.update_document_status(
            db, document_id, DocumentStatus.FAILED, error_message=str(e)
        )
        if staged_path:
            StorageManager.delete_staged_file(staged_path)
        return {"status": "failed", "document_id": document_id, "error": str(e)}
    finally:
        db.close()

@celery_app.task(name="async_ingest_connector_document_task")
def async_ingest_connector_document_task(
    document_id: str,
    job_id: str,
    filename: str,
    staged_path: str,
    org_id: str,
    department_id: Optional[str],
    uploader_id: str,
    access_level: str,
    mime_type: Optional[str]
):
    """
    Subtask: Ingests a single external connector document concurrently in the worker pool.
    """
    db, orchestrator = get_services()
    try:
        DocumentRepository.update_document_status(db, document_id, DocumentStatus.PROCESSING)
        file_bytes = StorageManager.read_staged_file(staged_path)

        chunk_count = orchestrator.ingest_document(
            filename=filename,
            file_bytes=file_bytes,
            document_id=document_id,
            org_id=org_id,
            department_id=department_id,
            uploader_id=uploader_id,
            access_level=access_level,
            mime_type=mime_type,
            db=db
        )

        DocumentRepository.update_document_status(
            db, document_id, DocumentStatus.INDEXED, chunk_count=chunk_count
        )
        StorageManager.delete_staged_file(staged_path)
        return {"status": "success", "document_id": document_id, "chunks": chunk_count}
    except Exception as e:
        logger.error(f"[CONNECTOR WORKER FAILURE] Document {document_id} failed: {e}")
        DocumentRepository.update_document_status(
            db, document_id, DocumentStatus.FAILED, error_message=str(e)
        )
        StorageManager.delete_staged_file(staged_path)
        return {"status": "failed", "document_id": document_id, "error": str(e)}
    finally:
        db.close()

@celery_app.task(name="async_execute_ingestion_job_task")
def async_execute_ingestion_job_task(job_id: str):
    """
    Orchestrates connector ingestion with Change Data Capture (CDC),
    Claim-Check staging, deletion tracking, and parallel fan-out execution.
    """
    db, orchestrator = get_services()
    job = db.query(IngestionJob).filter(IngestionJob.id == job_id).first()
    if not job:
        db.close()
        return {"status": "error", "message": f"Job {job_id} not found"}

    try:
        job.status = IngestionJobStatus.RUNNING
        db.commit()

        # 1. Decrypt connection config and instantiate connector
        decrypted_config = decrypt_connection_config(job.connection_config)
        connector = ConnectorRegistry.get_connector(job.source_type, decrypted_config)

        # 2. Discovery & Change Data Capture (CDC)
        active_external_ids = set()
        subtasks = []
        skipped_count = 0

        for raw_doc in connector.fetch_documents():
            external_id = raw_doc.metadata.get("external_id") or raw_doc.doc_id
            active_external_ids.add(external_id)

            file_hash = hashlib.sha256(raw_doc.content_bytes).hexdigest()
            last_mod = raw_doc.metadata.get("last_modified")
            ext_mod_dt = None
            if last_mod:
                try:
                    if isinstance(last_mod, datetime):
                        ext_mod_dt = last_mod
                    else:
                        ext_mod_dt = datetime.fromisoformat(str(last_mod).replace("Z", "+00:00")).replace(tzinfo=None)
                except Exception:
                    ext_mod_dt = None

            # Check if document already exists for this job
            existing_doc = DocumentRepository.get_document_by_external_id(
                db=db,
                org_id=job.org_id,
                job_id=job_id,
                external_id=external_id
            )

            # If unchanged, skip to save embedding computation
            if existing_doc and existing_doc.file_hash == file_hash and existing_doc.status == DocumentStatus.INDEXED:
                skipped_count += 1
                continue

            # If updating or new, create/update DB record
            if existing_doc:
                doc_record = existing_doc
                doc_record.file_hash = file_hash
                doc_record.file_size_bytes = len(raw_doc.content_bytes)
                doc_record.status = DocumentStatus.PENDING
                doc_record.external_last_modified = ext_mod_dt
                db.commit()
            else:
                doc_record = DocumentRepository.create_document(
                    db=db,
                    org_id=job.org_id,
                    department_id=job.department_id,
                    uploader_id=job.created_by_id or "system_admin",
                    filename=raw_doc.filename,
                    file_hash=file_hash,
                    file_size_bytes=len(raw_doc.content_bytes),
                    mime_type=raw_doc.mime_type or "application/octet-stream",
                    access_level=job.access_level,
                    job_id=job_id,
                    external_id=external_id,
                    external_last_modified=ext_mod_dt
                )

            # Stage file to disk (Claim-Check pattern)
            staged_path = StorageManager.save_staged_file(
                file_bytes=raw_doc.content_bytes,
                filename=raw_doc.filename,
                doc_id=doc_record.id,
                org_id=job.org_id
            )

            # Build subtask signature for parallel execution
            subtasks.append(
                async_ingest_connector_document_task.s(
                    document_id=doc_record.id,
                    job_id=job_id,
                    filename=raw_doc.filename,
                    staged_path=staged_path,
                    org_id=job.org_id,
                    department_id=job.department_id,
                    uploader_id=job.created_by_id or "system_admin",
                    access_level=job.access_level.value if hasattr(job.access_level, "value") else str(job.access_level),
                    mime_type=raw_doc.mime_type
                )
            )

        # 3. Purge documents deleted at external source
        purged_count = DocumentRepository.purge_deleted_external_documents(
            db=db,
            org_id=job.org_id,
            job_id=job_id,
            active_external_ids=active_external_ids,
            orchestrator=orchestrator
        )

        # 4. Fan-out execution across Celery workers
        processed_count = len(subtasks)
        if subtasks:
            job_group = group(subtasks)
            job_group.apply_async()

        job.status = IngestionJobStatus.COMPLETED
        job.documents_processed_count += processed_count
        job.last_run_at = datetime.utcnow()
        db.commit()

        logger.info(
            f"[ASYNC WORKER JOB] Completed job {job_id} ({job.name}): "
            f"{processed_count} dispatched, {skipped_count} unchanged skipped, {purged_count} deleted purged."
        )
        return {
            "status": "success",
            "job_id": job_id,
            "dispatched_count": processed_count,
            "skipped_count": skipped_count,
            "purged_count": purged_count
        }
    except Exception as e:
        logger.error(f"[ASYNC WORKER JOB FAILURE] Job {job_id} ({job.name}) failed: {e}")
        job.status = IngestionJobStatus.FAILED
        job.error_message = str(e)
        db.commit()
        return {"status": "failed", "job_id": job_id, "error": str(e)}
    finally:
        db.close()
