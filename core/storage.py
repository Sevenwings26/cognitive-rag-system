# core/storage.py
import os
import shutil
import logging
from typing import Optional, BinaryIO
from core.config import settings

logger = logging.getLogger("storage_manager")

class StorageManager:
    """
    Manages local disk/shared volume staging for the Claim-Check pattern.
    Avoids transmitting large Base64-encoded file payloads through Redis Celery task queues.
    """
    STAGING_DIR = getattr(settings, "STORAGE_STAGING_DIR", "/app/storage/staging")

    @classmethod
    def ensure_staging_dir(cls, org_id: Optional[str] = None) -> str:
        target_dir = os.path.join(cls.STAGING_DIR, org_id) if org_id else cls.STAGING_DIR
        os.makedirs(target_dir, exist_ok=True)
        return target_dir

    @classmethod
    def save_staged_file(cls, file_bytes: bytes, filename: str, doc_id: str, org_id: Optional[str] = None) -> str:
        target_dir = cls.ensure_staging_dir(org_id)
        safe_filename = "".join(c for c in filename if c.isalnum() or c in (".", "_", "-"))
        filepath = os.path.join(target_dir, f"{doc_id}_{safe_filename}")
        with open(filepath, "wb") as f:
            f.write(file_bytes)
        logger.info(f"[STORAGE] Staged {len(file_bytes)} bytes for doc {doc_id} at {filepath}")
        return filepath

    @classmethod
    def read_staged_file(cls, filepath: str) -> bytes:
        if not os.path.exists(filepath):
            raise FileNotFoundError(f"Staged file not found at path: {filepath}")
        with open(filepath, "rb") as f:
            return f.read()

    @classmethod
    def delete_staged_file(cls, filepath: str) -> bool:
        try:
            if os.path.exists(filepath):
                os.remove(filepath)
                logger.info(f"[STORAGE] Purged staged file: {filepath}")
                return True
        except Exception as e:
            logger.warning(f"[STORAGE] Error deleting staged file {filepath}: {e}")
        return False

    @classmethod
    def purge_staged_files_for_doc(cls, doc_id: str, org_id: Optional[str] = None) -> int:
        target_dir = cls.ensure_staging_dir(org_id)
        purged = 0
        try:
            if os.path.exists(target_dir):
                for fname in os.listdir(target_dir):
                    if fname.startswith(f"{doc_id}_"):
                        fpath = os.path.join(target_dir, fname)
                        if os.path.isfile(fpath):
                            os.remove(fpath)
                            purged += 1
                            logger.info(f"[STORAGE] Purged staged file for doc {doc_id}: {fname}")
        except Exception as e:
            logger.warning(f"[STORAGE] Error cleaning staged files for doc {doc_id}: {e}")
        return purged
