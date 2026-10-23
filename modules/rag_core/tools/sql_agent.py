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
        "You are an expert Enterprise Database Engineer and Relational SQL Specialist.\n"
        "Your task is to generate a single, highly accurate, read-only SQL query for the specified target database dialect.\n\n"
        "CORE RELATIONAL ARCHITECTURE & SECURITY RULES:\n"
        "1. READ-ONLY INVARIANT: Write ONLY a valid SELECT query. NEVER generate INSERT, UPDATE, DELETE, DROP, ALTER, TRUNCATE, EXEC, MERGE, or multiple stacked statements.\n"
        "2. CLOSED-WORLD SCHEMA BOUNDARY: Use ONLY the tables, views, and columns explicitly defined in the Schema Context. NEVER invent tables, join unlisted databases, or assume unverified relational objects exist.\n"
        "3. ZERO-FABRICATION & IDENTITY GROUNDING:\n"
        "   - You operate strictly under a closed-world assumption: filter by specific IDs, codes, names, or values ONLY when they are explicitly provided in the user inquiry or in [CONTEXT BINDINGS].\n"
        "   - NEVER fabricate, guess, or assume arbitrary entity IDs (e.g. primary keys, customer numbers, account codes) in WHERE clauses.\n"
        "   - When an inquiry uses first-person or subjective references (e.g. 'my organization', 'my account', 'our company') without an explicit identifier or context binding, DO NOT invent a hypothetical entity ID. Query the entity table generally (e.g. SELECT DISTINCT or top records) or filter by attributes explicitly mentioned in the inquiry.\n"
        "4. CONTEXT BINDINGS INTEGRATION: If [CONTEXT BINDINGS] are provided in the inquiry, integrate those exact primary/foreign key predicates directly into your WHERE or JOIN clauses to accurately anchor the entity scope.\n"
        "5. SCHEMA SEMANTICS & INTROSPECTION:\n"
        "   - Inspect table definitions, column types, comments, and annotations in the Schema Context to accurately identify entity concepts (e.g. organizations, employers, accounts, transactions, status codes).\n"
        "   - When asked for distinct or alternative entities (e.g. 'apart from X, which other...'), apply negative filtering on the relevant attribute (e.g. WHERE attribute NOT ILIKE '%<excluded>%' AND attribute IS NOT NULL) rather than combining positive and negative filters.\n"
        "   - Differentiate corporate/employer entities from internal staff/employee records by checking table and column descriptions in the schema.\n"
        "6. DIALECT PRECISION & OPTIMIZATION:\n"
        "   - Comply strictly with the target database dialect (PostgreSQL, MySQL, Oracle, MSSQL/TSQL).\n"
        "   - Apply dialect-appropriate limit clauses (LIMIT for PostgreSQL/MySQL, TOP for MSSQL, ROWNUM/FETCH FIRST for Oracle) on large result sets.\n"
        "   - When transactions, ledger activity, or event records are requested, return recent detail rows (ordered by timestamp/date descending) so downstream synthesis can compute totals, trends, or inspect individual entries.\n"
        "7. RELATIONAL AGGREGATION & PROJECTION INTEGRITY:\n"
        "   - NEVER combine unaggregated detail columns with aggregate functions (SUM, AVG, COUNT, MAX, MIN) in the same SELECT statement without an explicit GROUP BY clause.\n"
        "   - For entity detail requests that also ask for summary metrics (e.g. account list and total balance), project the individual entity records with their respective metric fields; the downstream analytical layer will compute aggregates across rows.\n"
        "   - In UNION / UNION ALL queries, ensure all subqueries share identical projection arity and compatible data types, and apply any ORDER BY at the top level using unqualified column names.\n"
        "8. OUTPUT FORMAT: Return ONLY the raw SQL query. Do NOT include markdown code blocks, backticks, conversational preamble, or reasoning commentary."
    )

    @classmethod
    def clean_sql_output(cls, raw_text: str) -> str:
        """Strips markdown code blocks, backticks, and extraneous whitespace from LLM output."""
        cleaned = raw_text.strip()
        if "```" in cleaned:
            # Extract content inside markdown code block if present to isolate SQL from trailing commentary
            match = re.search(r"```(?:sql)?\s*([\s\S]*?)\s*```", cleaned, flags=re.IGNORECASE)
            if match:
                cleaned = match.group(1).strip()
            else:
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
                retry_hint = ""
                if "does not exist" in last_error.lower() or "invalid object name" in last_error.lower():
                    retry_hint = "\nNote: The referenced table or object does NOT exist in this database schema. Use ONLY the valid tables listed in the Schema Context and apply any [CONTEXT BINDINGS] directly."
                prompt = (
                    f"=== Target Database Dialect ===\n{canonical_dialect.upper()}\n\n"
                    f"=== Schema Context (Table DDLs) ===\n{schema_context}\n\n"
                    f"=== User Inquiry ===\n{user_query}\n\n"
                    f"=== Previous Failed SQL ===\n{generated_sql}\n\n"
                    f"=== Execution Error Diagnostic ===\n{last_error}{retry_hint}\n\n"
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
