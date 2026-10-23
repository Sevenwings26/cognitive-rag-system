# modules/connectors/document_profiler.py
import re
import logging
from typing import List, Dict, Any, Optional
from modules.governance.domain.catalog_models import DocumentSourceProfile
from modules.connectors.sources.databases.profiler_rules import StructuralSchemaProfiler

logger = logging.getLogger("document_profiler")

class DocumentSourceProfiler:
    """
    Profiles unstructured repositories and cloud storage sources (S3, SharePoint, Google Drive),
    as well as user-uploaded session documents (PDF, DOCX, CSV, Excel).
    Extracts container taxonomy, topic tags, and schema headers for tabular files.
    """

    TABULAR_EXTENSIONS = {".csv", ".tsv", ".xlsx", ".xls", ".parquet"}

    @classmethod
    def profile_source(
        cls,
        source_type: str,
        source_name: str,
        job_id: Optional[str] = None,
        file_paths: Optional[List[str]] = None,
        bucket_or_site_name: Optional[str] = None,
        sample_tabular_headers: Optional[Dict[str, List[str]]] = None
    ) -> DocumentSourceProfile:
        """
        Creates a structured DocumentSourceProfile from file paths, containers, and tabular headers.
        """
        file_paths = file_paths or []
        container_names = []
        if bucket_or_site_name:
            container_names.append(bucket_or_site_name)

        topic_tags = set()
        file_types = set()
        all_entity_keys = set()

        # Extract path tokens, folder categories, and extensions
        for path in file_paths:
            parts = re.split(r"[/\\]", path.strip())
            # Capture file extension
            if "." in parts[-1]:
                ext = parts[-1].split(".")[-1].lower()
                file_types.add(ext)

            # Folder hierarchies indicate thematic topic containers
            for folder in parts[:-1]:
                clean_folder = folder.strip().lower()
                if clean_folder and clean_folder not in ("data", "files", "uploads", "documents", "shared", "root"):
                    topic_tags.add(clean_folder)

        # Tabular files: Profile column headers
        if sample_tabular_headers:
            for fname, headers in sample_tabular_headers.items():
                for h in headers:
                    if StructuralSchemaProfiler.is_entity_identifier(h):
                        all_entity_keys.add(h)

        return DocumentSourceProfile(
            job_id=job_id,
            source_type=source_type.upper(),
            source_name=source_name,
            container_names=container_names,
            topic_tags=sorted(list(topic_tags))[:20],
            file_types=sorted(list(file_types)),
            entity_keys=sorted(list(all_entity_keys))
        )

    @classmethod
    def profile_uploaded_file(
        cls,
        filename: str,
        headers: Optional[List[str]] = None
    ) -> Dict[str, Any]:
        """
        Generates lightweight profiling metadata for in-chat uploaded files.
        """
        ext = filename.split(".")[-1].lower() if "." in filename else ""
        entity_keys = []
        metric_keys = []

        if headers and ext in ("csv", "tsv", "xlsx", "xls"):
            for h in headers:
                if StructuralSchemaProfiler.is_entity_identifier(h):
                    entity_keys.append(h)
                elif StructuralSchemaProfiler.is_metric_column(h):
                    metric_keys.append(h)

        return {
            "filename": filename,
            "extension": ext,
            "is_tabular": ext in ("csv", "tsv", "xlsx", "xls"),
            "entity_keys": entity_keys,
            "metric_keys": metric_keys
        }
