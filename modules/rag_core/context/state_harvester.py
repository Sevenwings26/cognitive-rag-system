# modules/rag_core/context/state_harvester.py
import re
import logging
from typing import List, Dict, Any, Optional

from modules.rag_core.context.models import SessionWorkingMemory, EntityScope

logger = logging.getLogger("state_harvester")

class StateHarvester:
    """
    Universal Post-Turn State Harvester.
    Extracts high-value business entities, database primary keys, account identifiers,
    quantitative financial metrics, and document citations from execution results,
    enriching the SessionWorkingMemory Blackboard for subsequent turns.
    """

    KEY_ENTITY_FIELDS = {
        "customer_id", "customer_number", "bvn", "nin",
        "account_id", "account_number",
        "loan_id", "loan_account_number",
        "transaction_id", "transaction_reference"
    }

    KEY_METRIC_FIELDS = {
        "current_balance", "available_balance", "minimum_balance",
        "principal_amount", "outstanding_principal", "interest_rate",
        "approved_amount", "total", "inflow", "balance"
    }

    @classmethod
    def harvest(
        cls,
        memory: SessionWorkingMemory,
        strategy_name: str,
        query: str,
        answer: str,
        sources: List[Dict[str, Any]],
        extra_meta: Optional[Dict[str, Any]] = None
    ) -> SessionWorkingMemory:
        """
        Extracts entities and context facts from the executed turn into working memory.
        """
        memory.last_strategy = strategy_name
        memory.turn_count += 1

        if strategy_name == "system_meta":
            memory.evict_for_topic_shift(EntityScope.SYSTEM_META)
            return memory

        extra_meta = extra_meta or {}

        # 1. Harvest from SQL Structured Rows
        rows = extra_meta.get("rows", [])
        target_db = extra_meta.get("database_name")
        if target_db:
            memory.last_target_database = target_db

        is_multi_row = len(rows) > 1
        if is_multi_row:
            memory.scope = EntityScope.COLLECTION.value
        elif len(rows) == 1:
            memory.scope = EntityScope.INDIVIDUAL.value

        for row in rows[:10]:
            if not isinstance(row, dict):
                continue

            # Check entity IDs - only bind singular customer/account ID if single row
            if not is_multi_row:
                for field in cls.KEY_ENTITY_FIELDS:
                    if field in row and row[field] is not None:
                        memory.active_entities[field] = row[field]
                        if field == "customer_id":
                            memory.primary_anchor_id = row[field]
            else:
                # In multi-row collection, preserve primary_anchor_id if present
                if memory.primary_anchor_id and "customer_id" in row and row["customer_id"] == memory.primary_anchor_id:
                    memory.active_entities["customer_id"] = memory.primary_anchor_id

            # Check person / corporate names
            first_name = row.get("first_name")
            last_name = row.get("last_name")
            if first_name and last_name:
                full_name = f"{first_name} {last_name}".strip()
                if full_name and full_name not in memory.active_names:
                    memory.active_names.append(full_name)
            elif row.get("customer_name"):
                name = str(row["customer_name"]).strip()
                if name and name not in memory.active_names:
                    memory.active_names.append(name)

            # Check quantitative metrics
            for metric in cls.KEY_METRIC_FIELDS:
                if metric in row and row[metric] is not None:
                    memory.active_metrics[metric] = str(row[metric])

        # 2. Extract Entities from Query via Regex
        # BVN regex (11 digits starting with 9 or standard 11-digit pattern)
        if memory.scope not in (EntityScope.AGGREGATE.value, EntityScope.SYSTEM_META.value):
            bvn_match = re.search(r"\b(9\d{10}|\d{11})\b", query)
            if bvn_match and "bvn" not in memory.active_entities:
                memory.active_entities["bvn"] = bvn_match.group(1)

            # Explicit customer_id pattern in query or answer (only for single row / individual inquiry)
            if not is_multi_row:
                cid_match = re.search(r"(?:customer[_\s]?id|id)\s*[:=]\s*(\d+)", query + " " + answer, re.IGNORECASE)
                if cid_match and "customer_id" not in memory.active_entities:
                    try:
                        cid_val = int(cid_match.group(1))
                        memory.active_entities["customer_id"] = cid_val
                        memory.primary_anchor_id = cid_val
                    except ValueError:
                        pass

        # 3. Harvest Document Citations from RAG Sources
        for s in sources:
            fname = s.get("filename")
            if fname and not fname.startswith("schema_") and fname not in memory.active_documents:
                memory.active_documents.append(fname)

        # Bound active documents to most recent 5
        if len(memory.active_documents) > 5:
            memory.active_documents = memory.active_documents[-5:]

        # Bound active names to most recent 3
        if len(memory.active_names) > 3:
            memory.active_names = memory.active_names[-3:]

        logger.info(
            f"[STATE HARVESTER] Turn {memory.turn_count} recorded. "
            f"Entities: {list(memory.active_entities.keys())}, "
            f"Names: {memory.active_names}, "
            f"Metrics: {list(memory.active_metrics.keys())}"
        )

        return memory
