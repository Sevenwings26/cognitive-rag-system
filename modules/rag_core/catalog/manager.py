# modules/rag_core/catalog/manager.py
import time
import json
import logging
from typing import Dict, Any, Optional, Set, Tuple

from core.redis_client import get_redis
from modules.governance.domain.catalog_models import (
    TenantCatalog,
    DatabaseProfile,
    DocumentSourceProfile
)

logger = logging.getLogger("catalog_manager")

class CatalogManager:
    """
    High-Performance Zero-I/O Two-Tier Catalog Manager.
    L1: In-process memory cache with TTL (< 0.05 ms hot path)
    L2: Distributed Redis cache (< 1.5 ms)
    Fallback: Database lookup via CatalogService
    """

    _L1_CACHE: Dict[str, Tuple[TenantCatalog, float]] = {}
    _TTL_SECONDS: float = 300.0  # 5 minutes L1 TTL

    @classmethod
    def get_tenant_catalog(cls, org_id: str, db=None) -> TenantCatalog:
        """
        Retrieves the TenantCatalog for an organization with two-tier caching.
        """
        if not org_id:
            return TenantCatalog(org_id="")

        now = time.time()

        # 1. L1 In-Memory Cache Check
        cached = cls._L1_CACHE.get(org_id)
        if cached:
            catalog, expiry = cached
            if now < expiry:
                return catalog

        # 2. L2 Distributed Redis Cache Check
        redis_client = get_redis()
        redis_key = f"tenant:{org_id}:catalog"

        if redis_client:
            try:
                raw_json = redis_client.get(redis_key)
                if raw_json:
                    catalog = TenantCatalog.model_validate_json(raw_json)
                    cls._L1_CACHE[org_id] = (catalog, now + cls._TTL_SECONDS)
                    return catalog
            except Exception as e:
                logger.warning(f"[CATALOG MANAGER] Redis read failed for {org_id}: {e}")

        # 3. L3 Relational DB Fetch
        catalog = None
        if db:
            from modules.governance.services.catalog_service import CatalogService
            try:
                catalog = CatalogService.get_tenant_catalog(db, org_id)
            except Exception as e:
                logger.warning(f"[CATALOG MANAGER] DB fetch failed for org {org_id}: {e}")

        if not catalog:
            # Fallback to empty default catalog if DB unavailable or empty
            catalog = TenantCatalog(org_id=org_id)

        # Hydrate L1 and L2
        cls._L1_CACHE[org_id] = (catalog, now + cls._TTL_SECONDS)
        if redis_client:
            try:
                redis_client.setex(redis_key, int(cls._TTL_SECONDS), catalog.model_dump_json())
            except Exception as e:
                logger.warning(f"[CATALOG MANAGER] Redis write failed for {org_id}: {e}")

        return catalog

    @classmethod
    def evict(cls, org_id: str) -> None:
        """
        Invalidates L1 and L2 cache for a tenant.
        """
        cls._L1_CACHE.pop(org_id, None)
        redis_client = get_redis()
        if redis_client:
            try:
                redis_client.delete(f"tenant:{org_id}:catalog")
            except Exception as e:
                logger.warning(f"[CATALOG MANAGER] Redis evict failed for {org_id}: {e}")
        logger.info(f"[CATALOG MANAGER] Evicted cache for tenant {org_id}")

    @classmethod
    def get_database_profile(cls, org_id: str, db_name: str, db=None) -> Optional[DatabaseProfile]:
        """
        Finds a database profile by name (case-insensitive) in the tenant catalog.
        """
        catalog = cls.get_tenant_catalog(org_id, db=db)
        db_lower = db_name.lower()
        for name, profile in catalog.databases.items():
            if name.lower() == db_lower:
                return profile
        return None

    @classmethod
    def get_all_entity_keys(cls, org_id: str, db=None) -> Set[str]:
        """
        Returns all known entity identifier field names across all connected databases and documents.
        """
        catalog = cls.get_tenant_catalog(org_id, db=db)
        keys = set()
        for db_prof in catalog.databases.values():
            keys.update(db_prof.all_entity_keys)
        for doc_prof in catalog.document_sources.values():
            keys.update(doc_prof.entity_keys)
        return keys

    @classmethod
    def get_all_metric_keys(cls, org_id: str, db=None) -> Set[str]:
        """
        Returns all known quantitative metric field names across all connected databases.
        """
        catalog = cls.get_tenant_catalog(org_id, db=db)
        keys = set()
        for db_prof in catalog.databases.values():
            keys.update(db_prof.all_metric_keys)
        return keys

    @classmethod
    def register_database_profile_in_memory(cls, org_id: str, profile: DatabaseProfile) -> None:
        """
        Helper for testing and dynamic registration without DB roundtrip.
        """
        catalog = cls._L1_CACHE.get(org_id, (TenantCatalog(org_id=org_id), 0))[0]
        catalog.databases[profile.database_name] = profile
        cls._L1_CACHE[org_id] = (catalog, time.time() + cls._TTL_SECONDS)
