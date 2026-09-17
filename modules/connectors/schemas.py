# modules/connectors/schemas.py
"""Knowledge Source Connector Schemas."""
from enum import Enum
from typing import Dict, Any, List, Optional
from pydantic import BaseModel, Field

class FieldType(str, Enum):
    TEXT = "text"
    PASSWORD = "password"
    NUMBER = "number"
    TEXTAREA = "textarea"
    BOOLEAN = "boolean"
    SELECT = "select"

class ConnectorCategory(str, Enum):
    DATABASES = "Databases"
    CLOUD_STORAGE = "Cloud & File Storage"
    PRODUCTIVITY = "Wikis & Productivity"

class ConnectorSourceType(str, Enum):
    POSTGRES_DB = "POSTGRES_DB"
    MYSQL_DB = "MYSQL_DB"
    ORACLE_DB = "ORACLE_DB"
    MSSQL_DB = "MSSQL_DB"
    S3_BUCKET = "S3_BUCKET"
    GOOGLE_DRIVE = "GOOGLE_DRIVE"
    SHAREPOINT = "SHAREPOINT"
    CONFLUENCE = "CONFLUENCE"
    NOTION = "NOTION"

class ConnectorFieldMeta(BaseModel):
    name: str
    title: str
    field_type: FieldType = FieldType.TEXT
    placeholder: Optional[str] = None
    default_value: Optional[Any] = None
    is_secret: bool = False
    required: bool = True
    description: Optional[str] = None
    options: Optional[List[Dict[str, str]]] = None

class ConnectorDescriptor(BaseModel):
    source_type: str
    name: str
    category: ConnectorCategory
    description: str
    icon: str
    fields: List[ConnectorFieldMeta]


# --- 1. Relational Databases Configs ---
class PostgreSQLConfig(BaseModel):
    host: str = Field(default="localhost", description="Database host or IP")
    port: int = Field(default=5432, description="Port (default 5432)")
    database: str = Field(description="PostgreSQL Database Name")
    username: str = Field(description="Database Username")
    password: str = Field(description="Database Password")
    table_name: Optional[str] = Field(default=None, description="Optional target table name")
    sql_query: Optional[str] = Field(default=None, description="Optional custom extraction SQL query")
    text_column: Optional[str] = Field(default=None, description="Optional primary text column for row vectorization")
    filename_column: Optional[str] = Field(default=None, description="Optional filename/ID column")
    target_schema: Optional[str] = Field(default="public", description="Target database schema name")
    allowed_tables: Optional[List[str]] = Field(default=None, description="Optional filter list of tables to reflect")
    ssl_mode: str = Field(default="prefer", description="SSLMode")

class MySQLConfig(BaseModel):
    host: str = Field(default="localhost", description="MySQL Host or IP")
    port: int = Field(default=3306, description="Port (default 3306)")
    database: str = Field(description="MySQL Database Name")
    username: str = Field(description="Database Username")
    password: str = Field(description="Database Password")
    table_name: Optional[str] = Field(default=None, description="Optional target table name")
    sql_query: Optional[str] = Field(default=None, description="Optional custom extraction SQL query")
    text_column: Optional[str] = Field(default=None, description="Optional primary text column for row vectorization")
    filename_column: Optional[str] = Field(default=None, description="Optional filename/ID column")
    target_schema: Optional[str] = Field(default=None, description="Target database schema name")
    allowed_tables: Optional[List[str]] = Field(default=None, description="Optional filter list of tables to reflect")

class OracleConfig(BaseModel):
    host: str = Field(default="localhost", description="Oracle Host")
    port: int = Field(default=1521, description="Port (default 1521)")
    service_name: str = Field(default="ORCLPDB1", description="Service Name or SID")
    username: str = Field(description="Database User")
    password: str = Field(description="Database Password")
    table_name: Optional[str] = Field(default=None, description="Optional target table name")
    sql_query: Optional[str] = Field(default=None, description="Optional custom extraction SQL query")
    text_column: Optional[str] = Field(default=None, description="Optional primary text column for row vectorization")
    filename_column: Optional[str] = Field(default=None, description="Optional filename/ID column")
    target_schema: Optional[str] = Field(default=None, description="Target schema name")
    allowed_tables: Optional[List[str]] = Field(default=None, description="Optional filter list of tables to reflect")

class MSSQLConfig(BaseModel):
    host: str = Field(default="localhost", description="SQL Server Host or Instance")
    port: int = Field(default=1433, description="Port (default 1433)")
    database: str = Field(description="Database Name")
    username: str = Field(description="SQL User Login")
    password: str = Field(description="Password")
    table_name: Optional[str] = Field(default=None, description="Optional target table name")
    sql_query: Optional[str] = Field(default=None, description="Optional custom extraction SQL query")
    text_column: Optional[str] = Field(default=None, description="Optional primary text column for row vectorization")
    filename_column: Optional[str] = Field(default=None, description="Optional filename/ID column")
    target_schema: Optional[str] = Field(default="dbo", description="Target database schema name")
    allowed_tables: Optional[List[str]] = Field(default=None, description="Optional filter list of tables to reflect")
    encrypt: bool = Field(default=False, description="Enable TLS Encryption")

# --- 2. Cloud & File Storage Configs ---

class S3Config(BaseModel):
    bucket_name: str = Field(description="S3 Bucket Name")
    region_name: str = Field(default="us-east-1", description="AWS Region")
    prefix: str = Field(default="", description="Optional S3 Folder prefix")
    aws_access_key_id: Optional[str] = Field(default=None, description="AWS Access Key ID")
    aws_secret_access_key: Optional[str] = Field(default=None, description="AWS Secret Access Key")
    endpoint_url: Optional[str] = Field(default=None, description="Custom Endpoint URL (e.g. MinIO)")

class GoogleDriveConfig(BaseModel):
    service_account_json: str = Field(description="Google Cloud Service Account JSON key")
    folder_id: Optional[str] = Field(default=None, description="Google Drive Folder ID")
    include_shared_drives: bool = Field(default=True, description="Include Shared Team Drives")

class SharePointConfig(BaseModel):
    tenant_id: str = Field(description="Azure AD Tenant ID")
    client_id: str = Field(description="Azure AD Application (Client) ID")
    client_secret: str = Field(description="Azure AD Client Secret")
    site_url: Optional[str] = Field(default=None, description="SharePoint Site URL or Site ID")
    site_id: Optional[str] = Field(default=None, description="SharePoint Site ID (e.g. domain,site-guid,web-guid)")
    folder_path: str = Field(default="/Shared Documents", description="Document Library Folder path (blank or /Shared Documents for all documents)")

# --- 3. Wikis & Productivity Configs ---

class ConfluenceConfig(BaseModel):
    base_url: str = Field(description="Confluence Base URL (e.g. https://company.atlassian.net/wiki)")
    space_key: str = Field(description="Confluence Space Key")
    user_email: str = Field(description="Atlassian User Email")
    api_token: str = Field(description="Atlassian API Token")

class NotionConfig(BaseModel):
    api_token: str = Field(description="Notion Internal Integration Token")
    database_id: Optional[str] = Field(default=None, description="Optional Notion Database ID")

# --- Request / Response Contract Models ---

class ConnectorTestRequest(BaseModel):
    source_type: str = Field(..., description="Target connector source_type")
    connection_config: Dict[str, Any] = Field(..., description="Connection credentials payload")

class ConnectorTestResponse(BaseModel):
    success: bool
    latency_ms: float
    message: str
    details: Optional[Dict[str, Any]] = None
