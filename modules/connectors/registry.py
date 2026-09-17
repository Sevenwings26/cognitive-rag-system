# modules/connectors/registry.py
"""Connector Registry and Factory for Enterprise Data Connectors"""
import logging
from typing import Dict, Any, List, Type, Optional
from modules.connectors.base import BaseConnector
from modules.connectors.schemas import (
    ConnectorCategory, ConnectorSourceType,
    ConnectorFieldMeta, ConnectorDescriptor, FieldType,
    PostgreSQLConfig, MySQLConfig, OracleConfig, MSSQLConfig,
    S3Config, GoogleDriveConfig, SharePointConfig,
    ConfluenceConfig, NotionConfig
)
# structured data sources
from modules.connectors.sources.databases.postgres_connector import PostgreSQLConnector
from modules.connectors.sources.databases.mysql_connector import MySQLConnector
from modules.connectors.sources.databases.oracle_connector import OracleDBConnector
from modules.connectors.sources.databases.mssql_connector import MSSQLConnector

# cloud storage
from modules.connectors.sources.google_drive_connector import GoogleDriveConnector
from modules.connectors.sources.sharepoint_connector import SharePointConnector
from modules.connectors.sources.notion_connector import NotionConnector
from modules.connectors.sources.confluence_connector import ConfluenceConnector
from modules.connectors.sources.s3_connector import S3Connector

logger = logging.getLogger("connector_registry")

class ConnectorRegistry:
    """System-Wide Registry and Factory for Enterprise Data Connectors"""

    @staticmethod
    def get_descriptor_list() -> List[ConnectorDescriptor]:
        return [
            # --- Databases ---
            ConnectorDescriptor(
                source_type="POSTGRES_DB",
                name="PostgreSQL Database",
                category=ConnectorCategory.DATABASES,
                description="Auto-reflect schemas for live Text-to-SQL or extract records from PostgreSQL 11+",
                icon="🐘",
                fields=[
                    ConnectorFieldMeta(name="host", title="Database Host", placeholder="localhost or db.internal", default_value="localhost", required=True),
                    ConnectorFieldMeta(name="port", title="Port", field_type=FieldType.NUMBER, default_value=5432, required=True),
                    ConnectorFieldMeta(name="database", title="Database Name", placeholder="knowledge_db", required=True),
                    ConnectorFieldMeta(name="username", title="Username", placeholder="postgres", required=True),
                    ConnectorFieldMeta(name="password", title="Password", field_type=FieldType.PASSWORD, is_secret=True, required=True),
                    ConnectorFieldMeta(name="table_name", title="Target Table Name (Optional)", placeholder="Leave blank to auto-reflect all tables", required=False, description="Leave blank for Auto Schema Reflection & Text-to-SQL, or specify a table name"),
                    ConnectorFieldMeta(name="sql_query", title="Custom Extraction SQL (Optional)", field_type=FieldType.TEXTAREA, placeholder="e.g. SELECT id, title, content FROM documents", required=False, description="Optional custom query for row-level vectorization"),
                    ConnectorFieldMeta(name="text_column", title="Text Column (Optional)", placeholder="content", required=False, description="Optional column containing text to embed"),
                    ConnectorFieldMeta(name="filename_column", title="Filename / ID Column (Optional)", placeholder="id", required=False)
                ]
            ),
            ConnectorDescriptor(
                source_type="MYSQL_DB",
                name="MySQL Knowledge Source",
                category=ConnectorCategory.DATABASES,
                description="Auto-reflect schemas for live Text-to-SQL or ingest from MySQL 8 / MariaDB",
                icon="🐬",
                fields=[
                    ConnectorFieldMeta(name="host", title="MySQL Host", placeholder="localhost", default_value="localhost", required=True),
                    ConnectorFieldMeta(name="port", title="Port", field_type=FieldType.NUMBER, default_value=3306, required=True),
                    ConnectorFieldMeta(name="database", title="Database Name", placeholder="articles_db", required=True),
                    ConnectorFieldMeta(name="username", title="Username", placeholder="root", required=True),
                    ConnectorFieldMeta(name="password", title="Password", field_type=FieldType.PASSWORD, is_secret=True, required=True),
                    ConnectorFieldMeta(name="table_name", title="Target Table Name (Optional)", placeholder="Leave blank to auto-reflect all tables", required=False, description="Leave blank for Auto Schema Reflection & Text-to-SQL"),
                    ConnectorFieldMeta(name="sql_query", title="Custom Extraction SQL (Optional)", field_type=FieldType.TEXTAREA, placeholder="e.g. SELECT id, title, content FROM articles", required=False, description="Optional custom query for row-level vectorization"),
                    ConnectorFieldMeta(name="text_column", title="Text Column (Optional)", placeholder="content", required=False),
                    ConnectorFieldMeta(name="filename_column", title="Filename / ID Column (Optional)", placeholder="id", required=False)
                ]
            ),
            ConnectorDescriptor(
                source_type="ORACLE_DB",
                name="Oracle Database Source",
                category=ConnectorCategory.DATABASES,
                description="Auto-reflect schemas or sync enterprise records from Oracle 19c/21c",
                icon="🏛️",
                fields=[
                    ConnectorFieldMeta(name="host", title="Oracle Host", placeholder="oracle-server.internal", default_value="localhost", required=True),
                    ConnectorFieldMeta(name="port", title="Port", field_type=FieldType.NUMBER, default_value=1521, required=True),
                    ConnectorFieldMeta(name="service_name", title="Service Name / SID", default_value="ORCLPDB1", required=True),
                    ConnectorFieldMeta(name="username", title="Database User", placeholder="system", required=True),
                    ConnectorFieldMeta(name="password", title="Password", field_type=FieldType.PASSWORD, is_secret=True, required=True),
                    ConnectorFieldMeta(name="table_name", title="Target Table Name (Optional)", placeholder="Leave blank to auto-reflect all tables", required=False, description="Leave blank for Auto Schema Reflection & Text-to-SQL"),
                    ConnectorFieldMeta(name="sql_query", title="Custom Extraction SQL (Optional)", field_type=FieldType.TEXTAREA, placeholder="e.g. SELECT id, title, content FROM enterprise_docs", required=False),
                    ConnectorFieldMeta(name="text_column", title="Text Column (Optional)", placeholder="content", required=False),
                    ConnectorFieldMeta(name="filename_column", title="Filename Column (Optional)", placeholder="id", required=False)
                ]
            ),
            ConnectorDescriptor(
                source_type="MSSQL_DB",
                name="Microsoft SQL Server (MSSQL)",
                category=ConnectorCategory.DATABASES,
                description="Auto-reflect schemas or ingest data from MSSQL and Azure SQL",
                icon="🗄️",
                fields=[
                    ConnectorFieldMeta(name="host", title="SQL Server Host", placeholder="mssql.internal", default_value="localhost", required=True),
                    ConnectorFieldMeta(name="port", title="Port", field_type=FieldType.NUMBER, default_value=1433, required=True),
                    ConnectorFieldMeta(name="database", title="Database Name", placeholder="CorporateKB", required=True),
                    ConnectorFieldMeta(name="username", title="SQL User", placeholder="sa", required=True),
                    ConnectorFieldMeta(name="password", title="Password", field_type=FieldType.PASSWORD, is_secret=True, required=True),
                    ConnectorFieldMeta(name="table_name", title="Target Table Name (Optional)", placeholder="Leave blank to auto-reflect all tables", required=False, description="Leave blank for Auto Schema Reflection & Text-to-SQL"),
                    ConnectorFieldMeta(name="sql_query", title="Custom Extraction SQL (Optional)", field_type=FieldType.TEXTAREA, placeholder="e.g. SELECT id, title, body FROM KnowledgeArticles", required=False),
                    ConnectorFieldMeta(name="text_column", title="Text Column (Optional)", placeholder="body", required=False),
                    ConnectorFieldMeta(name="filename_column", title="Filename Column (Optional)", placeholder="id", required=False)
                ]
            ),
            # --- Cloud & File Storage ---
            ConnectorDescriptor(
                source_type="S3_BUCKET",
                name="Amazon S3 Bucket",
                category=ConnectorCategory.CLOUD_STORAGE,
                description="Sync PDFs, Office docs, and markdown from AWS S3 or MinIO",
                icon="🪣",
                fields=[
                    ConnectorFieldMeta(name="bucket_name", title="S3 Bucket Name", placeholder="corporate-knowledge", required=True),
                    ConnectorFieldMeta(name="region_name", title="AWS Region", default_value="us-east-1", placeholder="us-east-1", required=True),
                    ConnectorFieldMeta(name="prefix", title="Folder Prefix", placeholder="docs/", required=False),
                    ConnectorFieldMeta(name="aws_access_key_id", title="Access Key ID", placeholder="AKIA... (leave empty if using IAM Role)", required=False),
                    ConnectorFieldMeta(name="aws_secret_access_key", title="Secret Access Key", field_type=FieldType.PASSWORD, is_secret=True, required=False),
                    ConnectorFieldMeta(name="endpoint_url", title="Custom Endpoint URL", placeholder="https://s3.custom.com (for MinIO/Wasabi)", required=False)
                ]
            ),
            ConnectorDescriptor(
                source_type="GOOGLE_DRIVE",
                name="Google Workspace Drive",
                category=ConnectorCategory.CLOUD_STORAGE,
                description="Ingest documents, sheets, and folders from Google Drive",
                icon="📁",
                fields=[
                    ConnectorFieldMeta(name="service_account_json", title="Google Service Account JSON", field_type=FieldType.TEXTAREA, is_secret=True, placeholder='{"type": "service_account", ...}', required=True),
                    ConnectorFieldMeta(name="folder_id", title="Target Folder ID", placeholder="1abycz... (blank for Root)", required=False),
                    ConnectorFieldMeta(name="include_shared_drives", title="Include Shared Team Drives", field_type=FieldType.BOOLEAN, default_value=True, required=False)
                ]
            ),
            ConnectorDescriptor(
                source_type="SHAREPOINT",
                name="Microsoft SharePoint & OneDrive",
                category=ConnectorCategory.CLOUD_STORAGE,
                description="Sync document libraries via Microsoft Graph API",
                icon="🏢",
                fields=[
                    ConnectorFieldMeta(name="tenant_id", title="Azure AD Tenant ID", placeholder="xxxx-xxxx-xxxx", required=True),
                    ConnectorFieldMeta(name="client_id", title="Application (Client) ID", placeholder="yyyy-yyyy-yyyy", required=True),
                    ConnectorFieldMeta(name="client_secret", title="Client Secret Value", field_type=FieldType.PASSWORD, is_secret=True, required=True),
                    ConnectorFieldMeta(name="site_url", title="SharePoint Site URL or Site ID", placeholder="https://company.sharepoint.com/sites/knowledge or domain,site-guid,web-guid", required=True),
                    ConnectorFieldMeta(name="folder_path", title="Document Library / Folder", default_value="/Shared Documents", placeholder="/Shared Documents or /departments (leave default for all)", required=False)
                ]
            ),
            # --- Wikis & Productivity ---
            ConnectorDescriptor(
                source_type="CONFLUENCE",
                name="Atlassian Confluence Spaces",
                category=ConnectorCategory.PRODUCTIVITY,
                description="Sync technical wikis, runbooks, and policies from Confluence",
                icon="📘",
                fields=[
                    ConnectorFieldMeta(name="base_url", title="Confluence Base URL", placeholder="https://company.atlassian.net/wiki", required=True),
                    ConnectorFieldMeta(name="space_key", title="Space Key", placeholder="ENG", required=True),
                    ConnectorFieldMeta(name="user_email", title="Atlassian User Email", placeholder="admin@company.com", required=True),
                    ConnectorFieldMeta(name="api_token", title="API Token", field_type=FieldType.PASSWORD, is_secret=True, required=True)
                ]
            ),
            ConnectorDescriptor(
                source_type="NOTION",
                name="Notion Workspace",
                category=ConnectorCategory.PRODUCTIVITY,
                description="Ingest Notion databases, project docs, and pages",
                icon="📓",
                fields=[
                    ConnectorFieldMeta(name="api_token", title="Integration Token (secret_...)", field_type=FieldType.PASSWORD, is_secret=True, placeholder="secret_xxxxxxxxxxxxxxxxxxxxxxxxxxx", required=True),
                    ConnectorFieldMeta(name="database_id", title="Database / Page ID", placeholder="xxxx-xxxx-xxxx", required=False)
                ]
            )
        ]

    @staticmethod
    def get_connector(source_type: str, connection_config: Dict[str, Any]) -> BaseConnector:
        stype = source_type.upper()
        if stype in ["POSTGRES_DB", "POSTGRES", "POSTGRESQL", "RELATIONAL_DB"]:
            return PostgreSQLConnector(connection_config)
        elif stype in ["MYSQL_DB", "MYSQL"]:
            return MySQLConnector(connection_config)
        elif stype in ["ORACLE_DB", "ORACLE"]:
            return OracleDBConnector(connection_config)
        elif stype in ["MSSQL_DB", "MSSQL", "SQLSERVER"]:
            return MSSQLConnector(connection_config)
        elif stype in ["S3_BUCKET", "S3"]:
            return S3Connector(connection_config)
        elif stype in ["GOOGLE_DRIVE", "GSUITE", "GOOGLE_WORKSPACE"]:
            return GoogleDriveConnector(connection_config)
        elif stype in ["SHAREPOINT", "SHAREPOINTLOCAL", "ONEDRIVE"]:
            return SharePointConnector(connection_config)
        elif stype in ["CONFLUENCE", "ATLASSIAN_CONFLUENCE"]:
            return ConfluenceConnector(connection_config)
        elif stype in ["NOTION", "NOTION_WORKSPACE"]:
            return NotionConnector(connection_config)
        else:
            raise ValueError(f"Unsupported connector source_type: {source_type}")
