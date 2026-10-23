# modules/connectors/sources/databases/profiler_rules.py
import re
import logging
from typing import Optional, Dict, Any, List
from modules.governance.domain.catalog_models import ColumnSemanticRole

logger = logging.getLogger("structural_schema_profiler")

class StructuralSchemaProfiler:
    """
    Infers structural and semantic roles of database columns and tabular fields
    using deterministic schema heuristics (PK/FK status, data types, and structural suffix patterns).
    Operates without prior knowledge of the client's industry vertical.
    """

    ENTITY_SUFFIX_PATTERN = re.compile(
        r"(?:^|_)(id|pk|fk|no|number|num|code|ref|reference|key|guid|uuid|tag|mrn|msisdn|vin|imsi|imei|sku|upc)$",
        re.IGNORECASE
    )

    METRIC_KEYWORD_PATTERN = re.compile(
        r"(balance|amount|total|sum|rate|score|duration|volume|count|usage|price|cost|margin|"
        r"fee|charge|pressure|temp|temperature|speed|voltage|current|power|energy|latency|"
        r"loss|packet|bytes|mb|gb|tb|qty|quantity|val|value|pct|percentage|ratio)",
        re.IGNORECASE
    )

    NAME_KEYWORD_PATTERN = re.compile(
        r"^(first_name|last_name|full_name|name|display_name|title|subscriber_name|patient_name|customer_name|vendor_name|client_name)$",
        re.IGNORECASE
    )

    @classmethod
    def infer_column_role(
        cls,
        column_name: str,
        sql_type: str,
        is_pk: bool = False,
        is_fk: bool = False,
        distinct_count: Optional[int] = None
    ) -> ColumnSemanticRole:
        """
        Determines the semantic role for a given column.
        """
        col_lower = column_name.lower().strip()
        type_upper = str(sql_type).upper().strip()

        # 1. Primary keys and foreign keys are always entity identifiers
        if is_pk or is_fk:
            return ColumnSemanticRole.ENTITY_IDENTIFIER

        # 2. Match structural identifier suffix patterns
        if cls.ENTITY_SUFFIX_PATTERN.search(col_lower):
            return ColumnSemanticRole.ENTITY_IDENTIFIER

        # 3. Match explicit name and label patterns
        if cls.NAME_KEYWORD_PATTERN.match(col_lower):
            return ColumnSemanticRole.DESCRIPTIVE

        # 4. Floating point and precise decimal types are quantitative metrics
        if any(t in type_upper for t in ("NUMERIC", "DECIMAL", "FLOAT", "REAL", "DOUBLE", "MONEY")):
            return ColumnSemanticRole.METRIC_QUANTITATIVE

        # 5. Temporal types
        if any(t in type_upper for t in ("DATE", "TIME", "TIMESTAMP")):
            return ColumnSemanticRole.TEMPORAL

        # 6. Integer types
        if any(t in type_upper for t in ("INT", "BIGINT", "SMALLINT", "TINYINT")):
            if cls.METRIC_KEYWORD_PATTERN.search(col_lower):
                return ColumnSemanticRole.METRIC_QUANTITATIVE
            if distinct_count is not None and distinct_count <= 20:
                return ColumnSemanticRole.CATEGORICAL
            # If named like a metric, treat as quantitative metric
            if any(term in col_lower for term in ("year", "month", "day", "hour", "minute", "second", "age", "count", "num")):
                return ColumnSemanticRole.METRIC_QUANTITATIVE
            return ColumnSemanticRole.METRIC_QUANTITATIVE

        # 7. Low-cardinality text or enums
        if "ENUM" in type_upper or (distinct_count is not None and distinct_count <= 20):
            return ColumnSemanticRole.CATEGORICAL

        # 8. Check if string column matches metric keyword (e.g. string representations of amounts)
        if cls.METRIC_KEYWORD_PATTERN.search(col_lower):
            return ColumnSemanticRole.METRIC_QUANTITATIVE

        return ColumnSemanticRole.DESCRIPTIVE

    @classmethod
    def is_entity_identifier(cls, column_name: str, is_pk: bool = False, is_fk: bool = False) -> bool:
        """Fast helper to test if a column name represents an entity identifier."""
        if is_pk or is_fk:
            return True
        return bool(cls.ENTITY_SUFFIX_PATTERN.search(column_name.lower().strip()))

    @classmethod
    def is_metric_column(cls, column_name: str, sql_type: str = "") -> bool:
        """Fast helper to test if a column represents a quantitative metric."""
        type_upper = str(sql_type).upper()
        if any(t in type_upper for t in ("NUMERIC", "DECIMAL", "FLOAT", "REAL", "DOUBLE", "MONEY")):
            return True
        return bool(cls.METRIC_KEYWORD_PATTERN.search(column_name.lower().strip()))
