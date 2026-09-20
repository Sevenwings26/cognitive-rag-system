# modules/rag_core/tools/sql_agent.py
import re
import logging
from typing import Dict, Any, List, Optional
from sqlalchemy import create_engine

from modules.rag_core.providers.llm import BaseLLMService
from modules.connectors.security.sql_guard import (
    SQLSecurityGuard, InsecureQueryError, DatabaseSecurityTargetError
)

logger = logging.getLogger("dynamic_sql_agent")

class DynamicSQLAgent:
    """
    Dynamic Text-to-SQL Agent with AST Zero-Write Sandboxing and Self-Correction.
    Translates natural language questions into safe, dialect-tailored SELECT queries,
    validates them against security policies, executes inside read-only transactions,
    and formats real-time tabular results for the RAG orchestrator.
    """

    SYSTEM_INSTRUCTION = (
        "You are an expert Enterprise SQL Engineer and Data Analyst.\\n"
        "Your task is to write a single, highly accurate, read-only SQL query for the specified database dialect.\\n\\n"
        "RULES & CONSTRAINTS:\\n"
        "1. Write ONLY a valid SELECT statement. NEVER use INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, or EXEC.\\n"
        "2. Use ONLY tables and columns provided in the Schema Context below.\\n"
        "3. Match the specific SQL dialect syntax (e.g. PostgreSQL, MySQL, Oracle, MSSQL).\\n"
        "4. For date filtering or string matching, use dialect-standard functions (e.g. ILIKE for PostgreSQL/MySQL, LIKE for Oracle/MSSQL).\\n"
        "5. Limit large result sets appropriately (e.g. LIMIT 50 for Postgres/MySQL, TOP 50 for MSSQL, ROWNUM <= 50 for Oracle). When an inquiry asks for transaction activity or asks for BOTH a total/aggregate metric (e.g. total inflow, sum) AND an extreme (e.g. largest credit deposit), do NOT limit to 1 row; either compute the aggregates (e.g. SUM, MAX, COUNT) or SELECT all qualifying transactions up to LIMIT 50 so that both totals and specifics are available.\\n"
        "6. For entity lookup questions (e.g. 'Who is the customer...', 'Find customer...', 'Check account...'), SELECT relevant descriptive attributes (such as first_name, last_name, customer_type, verification_status, name) alongside IDs so the inquiry can be fully answered.\\n"
        "7. If [CONTEXT BINDINGS] are provided in the inquiry (e.g. customer_id = 1008, account_number = '0123456708'), use those explicit primary or foreign key values in your WHERE / JOIN clauses to locate the exact records.\\n"
        "8. Return ONLY the raw SQL query. Do NOT include markdown code blocks, explanations, or commentary.\\n"
        "9. SCHEMA SEMANTICS & VALUE PROFILING: Inspect table column definitions, database comments, and '-- Sample Column Values' annotations in the Schema Context to accurately disambiguate entity concepts:\\n"
        "   - When querying organizations, companies, employers, or client corporate affiliations, identify and match against columns whose sample values contain enterprise/corporate names (distinct from personal occupations or individual professions).\\n"
        "   - When asked for distinct organizations, companies, or industries, use SELECT DISTINCT on the corporate affiliation column and apply exclusion filters (e.g. WHERE <col> NOT ILIKE '%<excluded_name>%') against that same column.\\n"
        "   - Use personal occupation/job title columns ONLY when the user explicitly inquires about professions, roles, or job titles.\\n"
        "10. SQL SYNTAX & QUERY FORMULATION:\\n"
        "   - Query the primary table directly (e.g. SELECT ... FROM accounts WHERE customer_id = ...) without creating unnecessary UNION statements with other tables.\\n"
        "   - In queries with UNION / UNION ALL, any trailing ORDER BY clause MUST use unqualified output column names (e.g. 'ORDER BY account_number', NEVER 'ORDER BY a.account_number')."
    )

    @classmethod
    def clean_sql_output(cls, raw_text: str) -> str:
        """Strips markdown code blocks, backticks, and extraneous whitespace from LLM output."""
        cleaned = raw_text.strip()
        if "```" in cleaned:
            cleaned = re.sub(r"^```(?:sql)?\s*", "", cleaned, flags=re.IGNORECASE)
            cleaned = re.sub(r"\s*```$", "", cleaned)
        cleaned = cleaned.strip().rstrip(";")

        # Strip table aliases from ORDER BY in UNION queries (standard SQL requirement)
        if re.search(r"\bUNION\b", cleaned, re.IGNORECASE):
            cleaned = re.sub(
                r"(\bORDER\s+BY\s+)[a-zA-Z_]\w*\.([a-zA-Z_]\w*)",
                r"\1\2",
                cleaned,
                flags=re.IGNORECASE
            )

        return cleaned

    @classmethod
    def generate_and_execute_sql(
        cls,
        user_query: str,
        schema_context: str,
        dialect: str,
        db_url: str,
        llm_service: BaseLLMService,
        max_retries: int = 2
    ) -> Dict[str, Any]:
        """
        Executes the full Text-to-SQL lifecycle:
        1. LLM SQL Generation
        2. AST Security Validation (zero-write check)
        3. Engine-Level Read-Only Execution
        4. Self-Correction retry on execution error
        """
        canonical_dialect = SQLSecurityGuard.canonical_dialect(dialect)
        SQLSecurityGuard.validate_db_target(canonical_dialect, db_url)

        engine = create_engine(db_url)
        last_error = ""
        generated_sql = ""

        for attempt in range(max_retries + 1):
            # 1. Formulate Prompt
            if attempt == 0:
                prompt = (
                    f"=== Target Database Dialect ===\n{canonical_dialect.upper()}\n\n"
                    f"=== Schema Context (Table DDLs) ===\n{schema_context}\n\n"
                    f"=== User Natural Language Inquiry ===\n{user_query}\n\n"
                    f"Generate the exact SQL SELECT query to answer this inquiry accurately:"
                )
            else:
                prompt = (
                    f"=== Target Database Dialect ===\n{canonical_dialect.upper()}\n\n"
                    f"=== Schema Context (Table DDLs) ===\n{schema_context}\n\n"
                    f"=== User Inquiry ===\n{user_query}\n\n"
                    f"=== Previous Failed SQL ===\n{generated_sql}\n\n"
                    f"=== Execution Error Diagnostic ===\n{last_error}\n\n"
                    f"Fix the error and generate a corrected SQL SELECT query using valid tables and columns:"
                )

            # 2. LLM Call
            if hasattr(llm_service, "generate_text"):
                llm_response = llm_service.generate_text(
                    prompt=prompt,
                    system_instruction=cls.SYSTEM_INSTRUCTION
                )
            elif hasattr(llm_service, "generate_answer"):
                llm_response = llm_service.generate_answer(
                    system_instruction=cls.SYSTEM_INSTRUCTION,
                    user_prompt=prompt
                )
            else:
                raise AttributeError("LLM service has neither generate_text nor generate_answer")

            generated_sql = cls.clean_sql_output(llm_response)

            # 3. Security AST Guard Check
            try:
                validated_sql = SQLSecurityGuard.validate_query(generated_sql, dialect=canonical_dialect)
            except InsecureQueryError as sec_err:
                logger.warning(f"[SQL AGENT SECURITY REJECTION] Query blocked by AST guard: {sec_err}")
                return {
                    "status": "security_violation",
                    "error": str(sec_err),
                    "sql": generated_sql,
                    "rows": []
                }

            # 4. Engine-Level Read-Only Execution
            try:
                row_stream = SQLSecurityGuard.execute_read_only_query(
                    engine=engine,
                    dialect=canonical_dialect,
                    sql_query=validated_sql,
                    timeout_ms=15000,
                    batch_size=100
                )
                raw_rows = list(row_stream)
                rows = []
                for r in raw_rows:
                    safe_row = {}
                    for k, v in r.items():
                        if hasattr(v, "isoformat"):
                            safe_row[k] = v.isoformat()
                        elif isinstance(v, (int, float, bool, str)) or v is None:
                            safe_row[k] = v
                        else:
                            safe_row[k] = str(v)
                    rows.append(safe_row)

                columns = list(rows[0].keys()) if rows else []

                logger.info(f"[SQL AGENT SUCCESS] Executed SQL on {canonical_dialect} ({len(rows)} rows returned)")
                return {
                    "status": "success",
                    "sql": validated_sql,
                    "dialect": canonical_dialect,
                    "rows": rows,
                    "row_count": len(rows),
                    "columns": columns,
                    "attempts": attempt + 1
                }
            except Exception as exec_err:
                last_error = str(exec_err)
                logger.warning(f"[SQL AGENT RETRY {attempt+1}/{max_retries}] Query error: {last_error}")
                if attempt == max_retries:
                    return {
                        "status": "execution_failed",
                        "error": last_error,
                        "sql": generated_sql,
                        "rows": [],
                        "attempts": attempt + 1
                    }

        return {
            "status": "execution_failed",
            "error": last_error or "Unknown failure",
            "sql": generated_sql,
            "rows": []
        }
