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

        distinct_customers = {
            row.get("customer_id") for row in rows 
            if isinstance(row, dict) and row.get("customer_id") is not None
        }
        distinct_names = {
            f"{row.get('first_name', '')} {row.get('last_name', '')}".strip() 
            for row in rows 
            if isinstance(row, dict) and (row.get('first_name') or row.get('last_name'))
        }
        distinct_names.discard("")

        is_multi_customer = len(distinct_customers) > 1 or len(distinct_names) > 1
        
        if is_multi_customer:
            memory.scope = EntityScope.COLLECTION.value
        elif len(rows) == 1:
            memory.scope = EntityScope.INDIVIDUAL.value
        elif len(rows) > 1:
            # Multi-row result for a single customer (e.g., transaction ledger or accounts)
            if len(distinct_customers) == 1 or memory.primary_anchor_id or "customer_id" in memory.active_entities:
                memory.scope = EntityScope.INDIVIDUAL.value
            else:
                memory.scope = EntityScope.COLLECTION.value

        is_multi_row = len(rows) > 1
        is_collection = memory.scope == EntityScope.COLLECTION.value

        if is_collection and distinct_customers:
            memory.collection_ids = sorted(list(distinct_customers))
        elif not is_collection:
            memory.collection_ids.clear()

        for row in rows[:10]:
            if not isinstance(row, dict):
                continue

            # Check entity IDs - bind singular customer/account ID if not a multi-customer collection
            if not is_collection:
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
        # BVN regex (Strictly 11 digits regulatory format)
        if memory.scope not in (EntityScope.AGGREGATE.value, EntityScope.SYSTEM_META.value):
            bvn_match = re.search(r"\b(\d{11})\b", query)
            if bvn_match:
                bvn_val = bvn_match.group(1).strip()
                if len(bvn_val) == 11 and "bvn" not in memory.active_entities:
                    memory.active_entities["bvn"] = bvn_val

            # Explicit customer_id pattern in query or answer (only for individual inquiry)
            if not is_collection:
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

        # 4. Harvest Referenced Vendors from Document RAG or Queries
        vendor_matches = re.findall(r"\b(MTN|Samsung|Slot NG|PowerCert|Brand Forge|TalentLink|Vanguard Security|Pinnacle Learning)\b", query + " " + answer, re.IGNORECASE)
        for v in vendor_matches:
            v_clean = v.strip()
            if v_clean.upper() == "MTN":
                v_clean = "MTN Business Solutions"
            elif v_clean.upper() == "SAMSUNG":
                v_clean = "Samsung (Slot NG)"
            elif v_clean.upper() == "SLOT NG":
                v_clean = "Slot NG"
            if v_clean not in memory.active_vendors:
                memory.active_vendors.append(v_clean)

        generic_vendor_match = re.search(r"\b([a-zA-Z0-9_\-]{2,20})\s+vendor\b", query, re.IGNORECASE)
        if generic_vendor_match:
            gv = generic_vendor_match.group(1).strip()
            stopwords = {"that", "the", "this", "our", "a", "which", "each", "every", "another", "his", "her", "their", "first", "contact", "name"}
            if gv.lower() not in stopwords and gv not in memory.active_vendors:
                memory.active_vendors.append(gv)

        # Bound active collections
        if len(memory.active_documents) > 5:
            memory.active_documents = memory.active_documents[-5:]
        if len(memory.active_names) > 3:
            memory.active_names = memory.active_names[-3:]
        if len(memory.active_vendors) > 3:
            memory.active_vendors = memory.active_vendors[-3:]

        logger.info(
            f"[STATE HARVESTER] Turn {memory.turn_count} recorded. "
            f"Entities: {list(memory.active_entities.keys())}, "
            f"Names: {memory.active_names}, "
            f"Vendors: {memory.active_vendors}, "
            f"Metrics: {list(memory.active_metrics.keys())}"
        )

        return memory
