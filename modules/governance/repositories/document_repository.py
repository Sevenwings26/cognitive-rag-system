# modules/governance/repositories/document_repository.py
from typing import List, Optional, Set
from datetime import datetime
from sqlalchemy.orm import Session
from modules.auth.domain.models import AccessLevel
from modules.governance.domain.models import EnterpriseDocument, DocumentStatus, IngestionJob, IngestionJobStatus

class DocumentRepository:
    @staticmethod
    def create_document(
        db: Session,
        org_id: str,
        department_id: str,
        uploader_id: str,
        filename: str,
        file_hash: str,
        file_size_bytes: int,
        mime_type: str,
        access_level: AccessLevel,
        job_id: Optional[str] = None,
        external_id: Optional[str] = None,
        external_last_modified: Optional[datetime] = None
    ) -> EnterpriseDocument:
        doc = EnterpriseDocument(
            org_id=org_id,
            department_id=department_id if department_id else None,
            uploader_id=uploader_id if uploader_id else None,
            filename=filename,
            file_hash=file_hash,
            file_size_bytes=file_size_bytes,
            mime_type=mime_type,
            access_level=access_level,
            status=DocumentStatus.PENDING,
            job_id=job_id,
            external_id=external_id,
            external_last_modified=external_last_modified
        )
        db.add(doc)
        db.commit()
        db.refresh(doc)
        return doc

    @staticmethod
    def get_document_by_id(db: Session, doc_id: str, org_id: str) -> Optional[EnterpriseDocument]:
        return db.query(EnterpriseDocument).filter(
            EnterpriseDocument.id == doc_id,
            EnterpriseDocument.org_id == org_id
        ).first()

    @staticmethod
    def get_document_by_external_id(
        db: Session,
        org_id: str,
        job_id: str,
        external_id: str
    ) -> Optional[EnterpriseDocument]:
        return db.query(EnterpriseDocument).filter(
            EnterpriseDocument.org_id == org_id,
            EnterpriseDocument.job_id == job_id,
            EnterpriseDocument.external_id == external_id
        ).first()

    @staticmethod
    def list_job_documents(db: Session, org_id: str, job_id: str) -> List[EnterpriseDocument]:
        return db.query(EnterpriseDocument).filter(
            EnterpriseDocument.org_id == org_id,
            EnterpriseDocument.job_id == job_id
        ).all()

    @staticmethod
    def purge_deleted_external_documents(
        db: Session,
        org_id: str,
        job_id: str,
        active_external_ids: Set[str],
        orchestrator = None
    ) -> int:
        """Purges documents and vector points that no longer exist in the external source."""
        existing_docs = db.query(EnterpriseDocument).filter(
            EnterpriseDocument.org_id == org_id,
            EnterpriseDocument.job_id == job_id,
            EnterpriseDocument.external_id.isnot(None)
        ).all()

        purged_count = 0
        for doc in existing_docs:
            if doc.external_id not in active_external_ids:
                if orchestrator:
                    try:
                        orchestrator.delete_document_vectors(doc.id, org_id, db=db)
                    except Exception:
                        pass
                db.delete(doc)
                purged_count += 1

        if purged_count > 0:
            db.commit()
        return purged_count

    @staticmethod
    def update_document_status(
        db: Session,
        doc_id: str,
        status: DocumentStatus,
        chunk_count: Optional[int] = None,
        error_message: Optional[str] = None
    ):
        doc = db.query(EnterpriseDocument).filter(EnterpriseDocument.id == doc_id).first()
        if doc:
            doc.status = status
            if chunk_count is not None:
                doc.chunk_count = chunk_count
            if error_message is not None:
                doc.error_message = error_message
            db.commit()

    @staticmethod
    def list_accessible_documents(
        db: Session,
        org_id: str,
        department_id: Optional[str],
        user_id: str,
        user_role: str
    ) -> List[EnterpriseDocument]:
        query = db.query(EnterpriseDocument).filter(EnterpriseDocument.org_id == org_id)
        if user_role == "SUPER_ADMIN":
            return query.all()
        elif user_role == "DEPT_ADMIN":
            return query.filter(
                (EnterpriseDocument.department_id == department_id) |
                (EnterpriseDocument.access_level == AccessLevel.PUBLIC) |
                (EnterpriseDocument.uploader_id == user_id)
            ).all()
        else:
            return query.filter(
                (EnterpriseDocument.access_level == AccessLevel.PUBLIC) |
                ((EnterpriseDocument.department_id == department_id) & (EnterpriseDocument.access_level == AccessLevel.DEPARTMENT)) |
                (EnterpriseDocument.uploader_id == user_id)
            ).all()

    @staticmethod
    def delete_document(db: Session, doc_id: str, org_id: str) -> bool:
        doc = db.query(EnterpriseDocument).filter(
            EnterpriseDocument.id == doc_id,
            EnterpriseDocument.org_id == org_id
        ).first()
        if doc:
            db.delete(doc)
            db.commit()
            return True
        return False

    @staticmethod
    def create_ingestion_job(
        db: Session,
        org_id: str,
        department_id: str,
        created_by_id: str,
        name: str,
        source_type: str,
        access_level: AccessLevel,
        connection_config: dict,
        cron_schedule: Optional[str] = None
    ) -> IngestionJob:
        job = IngestionJob(
            org_id=org_id,
            department_id=department_id,
            created_by_id=created_by_id,
            name=name,
            source_type=source_type,
            access_level=access_level,
            connection_config=connection_config,
            cron_schedule=cron_schedule
        )
        db.add(job)
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def list_ingestion_jobs(db: Session, org_id: str) -> List[IngestionJob]:
        return db.query(IngestionJob).filter(IngestionJob.org_id == org_id).all()

    @staticmethod
    def get_ingestion_job(db: Session, job_id: str, org_id: str) -> Optional[IngestionJob]:
        return db.query(IngestionJob).filter(IngestionJob.id == job_id, IngestionJob.org_id == org_id).first()

    @staticmethod
    def update_ingestion_job(
        db: Session,
        job: IngestionJob,
        name: Optional[str] = None,
        access_level: Optional[AccessLevel] = None,
        department_id: Optional[str] = None,
        connection_config: Optional[dict] = None,
        cron_schedule: Optional[str] = None
    ) -> IngestionJob:
        if name is not None:
            job.name = name
        if access_level is not None:
            job.access_level = access_level
        if department_id is not None:
            job.department_id = department_id
        if connection_config is not None:
            job.connection_config = connection_config
        if cron_schedule is not None:
            job.cron_schedule = cron_schedule
        db.commit()
        db.refresh(job)
        return job

    @staticmethod
    def delete_ingestion_job(db: Session, job: IngestionJob) -> bool:
        db.delete(job)
        db.commit()
        return True

    @staticmethod
    def update_document_metadata(
        db: Session,
        doc_id: str,
        org_id: str,
        access_level: Optional[AccessLevel] = None,
        department_id: Optional[str] = None
    ) -> Optional[EnterpriseDocument]:
        doc = db.query(EnterpriseDocument).filter(
            EnterpriseDocument.id == doc_id,
            EnterpriseDocument.org_id == org_id
        ).first()
        if not doc:
            return None
        if access_level is not None:
            doc.access_level = access_level
        if department_id is not None:
            doc.department_id = department_id
        db.commit()
        db.refresh(doc)
        return doc
