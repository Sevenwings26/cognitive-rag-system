# modules/connectors/sources/databases/schema_reflector.py
import uuid
import logging
from typing import List, Dict, Any, Optional
from sqlalchemy import create_engine, MetaData, inspect
from sqlalchemy.schema import CreateTable

from modules.connectors.base import RawDocument
from modules.connectors.security.sql_guard import (
    SQLSecurityGuard, DatabaseSecurityTargetError
)
from modules.connectors.sources.databases.profiler_rules import StructuralSchemaProfiler
from modules.governance.domain.catalog_models import DatabaseProfile, TableProfile, ColumnProfile, ColumnSemanticRole

logger = logging.getLogger("schema_reflector")

class DatabaseSchemaReflector:
    """
    Introspects and reflects live database schemas using safe metadata inspection.
    Extracts table names, column data types, primary keys, and foreign keys,
    compiling them into clean DDL documents for Dynamic Text-to-SQL.
    """

    @classmethod
    def reflect_schema(
        cls,
        dialect: str,
        db_url: str,
        target_schema: Optional[str] = None,
        allowed_tables: Optional[List[str]] = None
    ) -> List[Dict[str, Any]]:
        """
        Reflects accessible tables and builds structured DDL metadata representations.
        """
        canonical = SQLSecurityGuard.canonical_dialect(dialect)
        SQLSecurityGuard.validate_db_target(canonical, db_url)

        connect_args = {"connect_timeout": 5} if canonical in ("postgresql", "mysql") else {}
        engine = create_engine(db_url, connect_args=connect_args)

        reflected_tables: List[Dict[str, Any]] = []

        try:
            metadata = MetaData(schema=target_schema)
            metadata.reflect(bind=engine, only=allowed_tables)

            for table_name, table in metadata.tables.items():
                short_name = table.name
                
                # Filter out system and migration tables
                if short_name.startswith(("pg_", "alembic_", "information_schema")):
                    continue

                # Compile clean CREATE TABLE DDL
                try:
                    ddl_str = str(CreateTable(table).compile(engine)).strip()
                except Exception:
                    # Fallback manual column extraction if dialect compilation issues arise
                    cols_desc = [f"  {c.name} {c.type}" for c in table.columns]
                    ddl_str = f"CREATE TABLE {short_name} (\n" + ",\n".join(cols_desc) + "\n);"

                pks = [c.name for c in table.primary_key.columns]
                fks = [
                    f"{fk.parent.name} -> {fk.column.table.name}.{fk.column.name}"
                    for fk in table.foreign_keys
                ]
                columns = [
                    {"name": c.name, "type": str(c.type), "nullable": c.nullable}
                    for c in table.columns
                ]

                # Dynamically sample distinct non-null values for text/categorical columns
                column_samples = {}
                try:
                    from sqlalchemy import text
                    with engine.connect() as conn:
                        for c in table.columns:
                            col_type_str = str(c.type).upper()
                            if any(t in col_type_str for t in ("VARCHAR", "CHAR", "TEXT", "STRING")):
                                safe_col = f'"{c.name}"' if canonical == "postgresql" else (f'[{c.name}]' if canonical == "mssql" else f'`{c.name}`')
                                safe_tbl = f'"{short_name}"' if canonical == "postgresql" else (f'[{short_name}]' if canonical == "mssql" else f'`{short_name}`')
                                if canonical == "mssql":
                                    query = f"SELECT DISTINCT TOP 3 {safe_col} FROM {safe_tbl} WHERE {safe_col} IS NOT NULL"
                                elif canonical == "oracle":
                                    query = f"SELECT DISTINCT {safe_col} FROM {safe_tbl} WHERE {safe_col} IS NOT NULL AND ROWNUM <= 3"
                                else:
                                    query = f"SELECT DISTINCT {safe_col} FROM {safe_tbl} WHERE {safe_col} IS NOT NULL LIMIT 3"
                                res = conn.execute(text(query)).fetchall()
                                samples = [str(r[0]) for r in res if r[0] is not None]
                                if samples:
                                    column_samples[c.name] = samples
                except Exception as sample_err:
                    logger.debug(f"[SCHEMA REFLECTOR] Value sampling skipped for {short_name}: {sample_err}")

                fk_cols = {fk.parent.name for fk in table.foreign_keys}
                column_roles = {}
                entity_keys = []
                metric_keys = []

                for c in table.columns:
                    role = StructuralSchemaProfiler.infer_column_role(
                        column_name=c.name,
                        sql_type=str(c.type),
                        is_pk=(c.name in pks),
                        is_fk=(c.name in fk_cols)
                    )
                    column_roles[c.name] = role.value
                    if role == ColumnSemanticRole.ENTITY_IDENTIFIER:
                        entity_keys.append(c.name)
                    elif role == ColumnSemanticRole.METRIC_QUANTITATIVE:
                        metric_keys.append(c.name)

                reflected_tables.append({
                    "table_name": short_name,
                    "full_table_name": table_name,
                    "schema": target_schema or table.schema,
                    "ddl": ddl_str,
                    "primary_keys": pks,
                    "foreign_keys": fks,
                    "columns": columns,
                    "column_names": [c["name"] for c in columns],
                    "column_roles": column_roles,
                    "entity_keys": entity_keys,
                    "metric_keys": metric_keys,
                    "column_samples": column_samples
                })

            logger.info(f"[SCHEMA REFLECTOR] Successfully reflected {len(reflected_tables)} tables for {canonical}")
            return reflected_tables
        except Exception as e:
            logger.error(f"[SCHEMA REFLECTOR] Failed to reflect schema: {e}")
            raise

    @classmethod
    def generate_schema_documents(
        cls,
        reflected_tables: List[Dict[str, Any]],
        dialect: str,
        source_type: str,
        db_name: str
    ) -> List[RawDocument]:
        """
        Converts reflected table metadata into lightweight RawDocument instances
        formatted for schema cataloging and LLM SQL Agent context.
        """
        schema_docs: List[RawDocument] = []

        for tbl in reflected_tables:
            table_name = tbl["table_name"]
            ddl_text = tbl["ddl"]
            pks = ", ".join(tbl["primary_keys"]) if tbl["primary_keys"] else "None"
            fks = ", ".join(tbl["foreign_keys"]) if tbl["foreign_keys"] else "None"

            sample_notes = []
            if tbl.get("column_samples"):
                sample_notes.append("-- Sample Column Values (Distinct data observations):")
                for col_name, samples in tbl["column_samples"].items():
                    sample_notes.append(f"--   {col_name}: {samples}")
            sample_str = "\n".join(sample_notes) + "\n\n" if sample_notes else ""

            structured_content = (
                f"-- Database Schema DDL for Table: {table_name}\n"
                f"-- Dialect: {dialect} | Database: {db_name}\n"
                f"-- Primary Keys: {pks}\n"
                f"-- Foreign Keys: {fks}\n\n"
                f"{sample_str}"
                f"{ddl_text}\n"
            )

            doc_id = str(uuid.uuid4())
            metadata = {
                "chunk_type": "sql_schema",
                "table_name": table_name,
                "database_name": db_name,
                "dialect": dialect,
                "source_type": source_type,
                "column_names": tbl["column_names"],
                "column_roles": tbl.get("column_roles", {}),
                "entity_keys": tbl.get("entity_keys", []),
                "metric_keys": tbl.get("metric_keys", []),
                "primary_keys": tbl["primary_keys"],
                "foreign_keys": tbl["foreign_keys"]
            }

            schema_docs.append(RawDocument(
                doc_id=doc_id,
                source_type=source_type,
                filename=f"schema_{table_name}.sql",
                content_bytes=structured_content.encode("utf-8"),
                mime_type="application/sql",
                metadata=metadata
            ))

        return schema_docs

    @classmethod
    def build_database_profile(
        cls,
        reflected_tables: List[Dict[str, Any]],
        job_id: str,
        db_name: str,
        dialect: str,
        domain_tags: Optional[Dict[str, List[str]]] = None
    ) -> DatabaseProfile:
        """
        Constructs a structured DatabaseProfile Pydantic model from reflected tables.
        """
        table_profiles: Dict[str, TableProfile] = {}
        all_entity_keys: List[str] = []
        all_metric_keys: List[str] = []

        for tbl in reflected_tables:
            tname = tbl["table_name"]
            pks = tbl.get("primary_keys", [])
            fks = tbl.get("foreign_keys", [])
            ent_keys = tbl.get("entity_keys", [])
            met_keys = tbl.get("metric_keys", [])

            all_entity_keys.extend(ent_keys)
            all_metric_keys.extend(met_keys)

            columns_dict: Dict[str, ColumnProfile] = {}
            for col in tbl.get("columns", []):
                cname = col["name"]
                ctype = col["type"]
                crole = tbl.get("column_roles", {}).get(cname, ColumnSemanticRole.DESCRIPTIVE.value)
                samples = tbl.get("column_samples", {}).get(cname, [])

                columns_dict[cname] = ColumnProfile(
                    name=cname,
                    role=ColumnSemanticRole(crole),
                    data_type=ctype,
                    is_primary_key=(cname in pks),
                    is_foreign_key=any(f.startswith(f"{cname} ->") for f in fks),
                    samples=samples
                )

            table_profiles[tname] = TableProfile(
                table_name=tname,
                schema_name=tbl.get("schema"),
                primary_entity_keys=ent_keys,
                metric_keys=met_keys,
                semantic_tags=[tname] + ent_keys[:3],
                columns=columns_dict
            )

        # Deduplicate while preserving order
        unique_entity_keys = list(dict.fromkeys(all_entity_keys))
        unique_metric_keys = list(dict.fromkeys(all_metric_keys))

        tags = domain_tags or {"primary": [db_name] + list(table_profiles.keys()), "secondary": []}

        return DatabaseProfile(
            job_id=job_id,
            database_name=db_name,
            dialect=dialect,
            domain_tags=tags,
            tables=table_profiles,
            all_entity_keys=unique_entity_keys,
            all_metric_keys=unique_metric_keys
        )

