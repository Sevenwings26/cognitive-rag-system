# modules/governance/domain/catalog_models.py
from enum import Enum
from typing import List, Dict, Any, Optional
from pydantic import BaseModel, Field

class ColumnSemanticRole(str, Enum):
    """Semantic functional role of a database or tabular column."""
    ENTITY_IDENTIFIER = "ENTITY_IDENTIFIER"     # Primary key, foreign key, or discrete entity ID
    METRIC_QUANTITATIVE = "METRIC_QUANTITATIVE" # Numerical measure, currency, score, or aggregate
    CATEGORICAL = "CATEGORICAL"                 # Low-cardinality status, type, or state enum
    TEMPORAL = "TEMPORAL"                       # Timestamp, date, or time marker
    DESCRIPTIVE = "DESCRIPTIVE"                 # Textual name, label, notes, or narrative

class ColumnProfile(BaseModel):
    """Structural and semantic profile of a specific column."""
    name: str
    role: ColumnSemanticRole = ColumnSemanticRole.DESCRIPTIVE
    data_type: str
    is_primary_key: bool = False
    is_foreign_key: bool = False
    foreign_target: Optional[str] = None       # e.g., "target_table.target_col"
    samples: List[str] = Field(default_factory=list)

class TableProfile(BaseModel):
    """Semantic profile for an individual relational table or structured dataset."""
    table_name: str
    schema_name: Optional[str] = None
    description: Optional[str] = None
    primary_entity_keys: List[str] = Field(default_factory=list)
    metric_keys: List[str] = Field(default_factory=list)
    semantic_tags: List[str] = Field(default_factory=list)
    columns: Dict[str, ColumnProfile] = Field(default_factory=dict)

class DatabaseProfile(BaseModel):
    """Dynamic domain profile for a registered database connector."""
    job_id: str
    database_name: str
    dialect: Optional[str] = None
    domain_tags: Dict[str, List[str]] = Field(default_factory=lambda: {"primary": [], "secondary": []})
    tables: Dict[str, TableProfile] = Field(default_factory=dict)
    all_entity_keys: List[str] = Field(default_factory=list)
    all_metric_keys: List[str] = Field(default_factory=list)

class DocumentSourceProfile(BaseModel):
    """Dynamic profile for cloud storage and unstructured/semi-structured repositories."""
    job_id: Optional[str] = None
    source_type: str                            # e.g. "S3", "SHAREPOINT", "GOOGLE_DRIVE", "SESSION_UPLOAD"
    source_name: str                            # e.g. "aws-s3-production", "corporate-sharepoint"
    container_names: List[str] = Field(default_factory=list) # e.g. ["telecom-cdr-bucket", "clinical-trials"]
    topic_tags: List[str] = Field(default_factory=list)      # e.g. ["policies", "contracts", "network-logs"]
    file_types: List[str] = Field(default_factory=list)      # e.g. ["pdf", "docx", "csv", "xlsx"]
    entity_keys: List[str] = Field(default_factory=list)     # Tabular column headers if spreadsheet/CSV

class TenantCatalog(BaseModel):
    """Complete tenant-level domain catalog loaded into cache."""
    org_id: str
    domain_vertical: str = "GENERIC"            # e.g. "BANKING", "TELECOM", "HEALTHCARE", "DEFENSE", "GENERIC"
    databases: Dict[str, DatabaseProfile] = Field(default_factory=dict)
    document_sources: Dict[str, DocumentSourceProfile] = Field(default_factory=dict)
    universal_synonyms: Dict[str, List[str]] = Field(default_factory=dict)
    global_regex_patterns: Dict[str, str] = Field(default_factory=dict)
