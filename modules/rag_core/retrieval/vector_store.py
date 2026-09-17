# modules/rag_core/retrieval/vector_store.py
import logging
from typing import List, Dict, Any, Optional
from qdrant_client import QdrantClient
from qdrant_client.models import (
    VectorParams, Distance, PointStruct, Filter,
    HnswConfigDiff, PayloadSchemaType, SearchParams
)
from core.config import settings

logger = logging.getLogger("vector_store_service")

class VectorStoreService:
    """
    High-Performance Qdrant Vector Store Service.
    Configured for sub-30ms HNSW retrieval, multi-tenant payload indexing,
    and RAM-cached vector representations.
    """
    _instance: Optional["VectorStoreService"] = None

    def __init__(self, client: Optional[QdrantClient] = None):
        if client:
            self.client = client
        elif settings.QDRANT_STORAGE == "server":
            self.client = QdrantClient(
                host=settings.QDRANT_HOST,
                port=settings.QDRANT_PORT,
                api_key=settings.QDRANT_API_KEY,
                timeout=10.0
            )
        else:
            self.client = QdrantClient(path=settings.QDRANT_PATH)

        self.collection_name = settings.QDRANT_COLLECTION
        self._ensure_collection_and_indexes()

    def _ensure_collection_and_indexes(self):
        """Initializes collection with HNSW parameters and creates payload indexes for instant filtered lookups."""
        try:
            collections = self.client.get_collections().collections
            exists = any(c.name == self.collection_name for c in collections)
            if not exists:
                logger.info(f"Creating optimized Qdrant collection '{self.collection_name}' (dim={settings.EMBEDDING_DIMENSION})...")
                self.client.create_collection(
                    collection_name=self.collection_name,
                    vectors_config=VectorParams(
                        size=settings.EMBEDDING_DIMENSION,
                        distance=Distance.COSINE,
                        on_disk=False  # Keep in RAM for sub-30ms retrieval SLA
                    ),
                    hnsw_config=HnswConfigDiff(
                        m=16,
                        ef_construct=100,
                        on_disk=False
                    )
                )

            # Ensure Payload Keyword Indexes for tenant & metadata filtering
            indexed_fields = {
                "org_id": PayloadSchemaType.KEYWORD,
                "department_id": PayloadSchemaType.KEYWORD,
                "access_level": PayloadSchemaType.KEYWORD,
                "uploader_id": PayloadSchemaType.KEYWORD,
                "session_id": PayloadSchemaType.KEYWORD,
                "document_id": PayloadSchemaType.KEYWORD,
            }
            for field, schema in indexed_fields.items():
                try:
                    self.client.create_payload_index(
                        collection_name=self.collection_name,
                        field_name=field,
                        field_schema=schema
                    )
                except Exception:
                    pass  # Already indexed or in-memory mock
        except Exception as e:
            logger.error(f"Error ensuring Qdrant collection and payload indexes: {e}")

    def upsert_chunks(self, points: List[PointStruct]):
        """Upserts a batch of point vectors into Qdrant."""
        if not points:
            return
        self.client.upsert(
            collection_name=self.collection_name,
            points=points
        )

    def search_vectors(
        self,
        query_vector: List[float],
        search_filter: Optional[Filter] = None,
        limit: int = 10,
        score_threshold: Optional[float] = None
    ) -> List[Dict[str, Any]]:
        """
        Executes vector similarity search with HNSW search tuning (ef=64).
        """
        hits = []
        # Sub-30ms HNSW search parameters
        search_params = SearchParams(hnsw_ef=64, exact=False)

        try:
            if hasattr(self.client, "query_points"):
                response = self.client.query_points(
                    collection_name=self.collection_name,
                    query=query_vector,
                    query_filter=search_filter,
                    limit=limit,
                    score_threshold=score_threshold,
                    search_params=search_params
                )
                hits = response.points if hasattr(response, "points") else response
            elif hasattr(self.client, "search"):
                hits = self.client.search(
                    collection_name=self.collection_name,
                    query_vector=query_vector,
                    query_filter=search_filter,
                    limit=limit,
                    score_threshold=score_threshold,
                    search_params=search_params
                )
        except Exception as e:
            logger.error(f"Error searching vectors in Qdrant collection '{self.collection_name}': {e}")
            return []

        return [
            {
                "id": getattr(hit, "id", None),
                "score": getattr(hit, "score", 0.0),
                "payload": getattr(hit, "payload", {}) or {}
            }
            for hit in hits
        ]

    def delete_by_filter(self, points_filter: Filter) -> bool:
        """Deletes points matching a specific filter."""
        try:
            self.client.delete(
                collection_name=self.collection_name,
                points_selector=points_filter
            )
            return True
        except Exception as e:
            logger.error(f"Error deleting vectors with filter: {e}")
            return False

    def set_payload_by_filter(self, points_filter: Filter, payload: Dict[str, Any]) -> bool:
        """Updates payload metadata for points matching a specific filter."""
        try:
            self.client.set_payload(
                collection_name=self.collection_name,
                payload=payload,
                points=points_filter
            )
            return True
        except Exception as e:
            logger.error(f"Error updating vector payload with filter: {e}")
            return False

    def get_collection_stats(self) -> Dict[str, Any]:
        """Returns collection size, point count, indexed vector count, and health status."""
        try:
            info = self.client.get_collection(self.collection_name)
            return {
                "status": "healthy",
                "collection_name": self.collection_name,
                "points_count": getattr(info, "points_count", 0),
                "indexed_vectors_count": getattr(info, "indexed_vectors_count", 0),
                "segments_count": getattr(info, "segments_count", 0),
                "status_str": str(getattr(info, "status", "ready"))
            }
        except Exception as e:
            return {
                "status": "unhealthy",
                "collection_name": self.collection_name,
                "error": str(e)
            }


_global_vector_store: Optional[VectorStoreService] = None

def get_vector_store_service() -> VectorStoreService:
    """Singleton accessor for VectorStoreService to reuse connection pool."""
    global _global_vector_store
    if _global_vector_store is None:
        _global_vector_store = VectorStoreService()
    return _global_vector_store
