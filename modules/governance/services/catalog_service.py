# modules/governance/services/catalog_service.py
import logging
from typing import Optional, Dict, Any, List
from sqlalchemy.orm import Session
from sqlalchemy import select

from modules.governance.domain.models import (
    DatabaseSemanticProfile,
    DocumentSourceProfileModel,
    TenantDomainCatalogModel,
    IngestionJob
)
from modules.governance.domain.catalog_models import (
    TenantCatalog,
    DatabaseProfile,
    DocumentSourceProfile,
    TableProfile,
    ColumnProfile
)

logger = logging.getLogger("catalog_service")

class CatalogService:
    """
    CRUD and synchronization service for Tenant Domain Catalogs and Semantic Profiles.
    """

    @classmethod
    def save_database_profile(
        cls,
        db: Session,
        org_id: str,
        profile: DatabaseProfile
    ) -> DatabaseSemanticProfile:
        """
        Persists or updates a DatabaseProfile in the database.
        """
        record = db.query(DatabaseSemanticProfile).filter(
            DatabaseSemanticProfile.org_id == org_id,
            DatabaseSemanticProfile.job_id == profile.job_id
        ).first()

        serialized_tables = {k: v.model_dump() for k, v in profile.tables.items()}

        if not record:
            record = DatabaseSemanticProfile(
                job_id=profile.job_id,
                org_id=org_id,
                database_name=profile.database_name,
                dialect=profile.dialect,
                domain_tags=profile.domain_tags,
                table_profiles=serialized_tables,
                all_entity_keys=profile.all_entity_keys,
                all_metric_keys=profile.all_metric_keys
            )
            db.add(record)
        else:
            record.database_name = profile.database_name
            record.dialect = profile.dialect
            record.domain_tags = profile.domain_tags
            record.table_profiles = serialized_tables
            record.all_entity_keys = profile.all_entity_keys
            record.all_metric_keys = profile.all_metric_keys

        db.commit()
        db.refresh(record)
        logger.info(f"[CATALOG SERVICE] Persisted DatabaseSemanticProfile for {profile.database_name} (org: {org_id})")
        return record

    @classmethod
    def save_document_source_profile(
        cls,
        db: Session,
        org_id: str,
        profile: DocumentSourceProfile
    ) -> DocumentSourceProfileModel:
        """
        Persists or updates a DocumentSourceProfile in the database.
        """
        record = None
        if profile.job_id:
            record = db.query(DocumentSourceProfileModel).filter(
                DocumentSourceProfileModel.org_id == org_id,
                DocumentSourceProfileModel.job_id == profile.job_id
            ).first()

        if not record:
            record = DocumentSourceProfileModel(
                job_id=profile.job_id,
                org_id=org_id,
                source_type=profile.source_type,
                source_name=profile.source_name,
                container_names=profile.container_names,
                topic_tags=profile.topic_tags,
                file_types=profile.file_types,
                entity_keys=profile.entity_keys
            )
            db.add(record)
        else:
            record.source_type = profile.source_type
            record.source_name = profile.source_name
            record.container_names = profile.container_names
            record.topic_tags = profile.topic_tags
            record.file_types = profile.file_types
            record.entity_keys = profile.entity_keys

        db.commit()
        db.refresh(record)
        logger.info(f"[CATALOG SERVICE] Persisted DocumentSourceProfile for {profile.source_name} (org: {org_id})")
        return record

    @classmethod
    def get_tenant_catalog(cls, db: Session, org_id: str) -> TenantCatalog:
        """
        Assembles a full TenantCatalog from database records.
        """
        try:
            # 1. Fetch domain catalog record
            cat_record = db.query(TenantDomainCatalogModel).filter(
                TenantDomainCatalogModel.org_id == org_id
            ).first()

            domain_vertical = cat_record.domain_vertical if cat_record else "GENERIC"
            universal_synonyms = cat_record.universal_synonyms if cat_record else {}
            global_regex_patterns = cat_record.global_regex_patterns if cat_record else {}

            # 2. Fetch database semantic profiles
            db_records = db.query(DatabaseSemanticProfile).filter(
                DatabaseSemanticProfile.org_id == org_id
            ).all()

            databases: Dict[str, DatabaseProfile] = {}
            for d in db_records:
                tables = {}
                for tname, tdata in (d.table_profiles or {}).items():
                    tables[tname] = TableProfile.model_validate(tdata)

                databases[d.database_name] = DatabaseProfile(
                    job_id=d.job_id,
                    database_name=d.database_name,
                    dialect=d.dialect,
                    domain_tags=d.domain_tags or {},
                    tables=tables,
                    all_entity_keys=d.all_entity_keys or [],
                    all_metric_keys=d.all_metric_keys or []
                )

            # 3. Fetch document source profiles
            doc_records = db.query(DocumentSourceProfileModel).filter(
                DocumentSourceProfileModel.org_id == org_id
            ).all()

            document_sources: Dict[str, DocumentSourceProfile] = {}
            for doc in doc_records:
                document_sources[doc.source_name] = DocumentSourceProfile(
                    job_id=doc.job_id,
                    source_type=doc.source_type,
                    source_name=doc.source_name,
                    container_names=doc.container_names or [],
                    topic_tags=doc.topic_tags or [],
                    file_types=doc.file_types or [],
                    entity_keys=doc.entity_keys or []
                )

            return TenantCatalog(
                org_id=org_id,
                domain_vertical=domain_vertical,
                databases=databases,
                document_sources=document_sources,
                universal_synonyms=universal_synonyms,
                global_regex_patterns=global_regex_patterns
            )
        except Exception as e:
            try:
                db.rollback()
            except Exception:
                pass
            logger.warning(f"[CATALOG SERVICE] Failed to query catalog from DB: {e}")
            return TenantCatalog(org_id=org_id)
