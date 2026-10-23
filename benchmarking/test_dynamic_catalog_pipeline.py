# benchmarking/test_dynamic_catalog_pipeline.py
import unittest
import uuid
import re

from modules.governance.domain.catalog_models import (
    ColumnSemanticRole,
    ColumnProfile,
    TableProfile,
    DatabaseProfile,
    DocumentSourceProfile,
    TenantCatalog
)
from modules.connectors.sources.databases.profiler_rules import StructuralSchemaProfiler
from modules.connectors.document_profiler import DocumentSourceProfiler
from modules.rag_core.catalog.manager import CatalogManager
from modules.rag_core.context.models import SessionWorkingMemory, EntityScope
from modules.rag_core.context.query_condenser import QueryCondenser
from modules.rag_core.context.state_harvester import StateHarvester
from modules.rag_core.orchestrator.strategies.sql_strategy import DynamicSQLStrategy
from modules.rag_core.orchestrator.query_planner import QueryPlanner
from modules.governance.domain.models import IngestionJob

class TestDynamicCatalogAndContextPipeline(unittest.TestCase):

    def test_structural_schema_profiler(self):
        """Verify structural column role inference across healthcare, telecom, and logistics."""
        # Healthcare
        self.assertEqual(
            StructuralSchemaProfiler.infer_column_role("patient_mrn", "VARCHAR(50)", is_pk=True),
            ColumnSemanticRole.ENTITY_IDENTIFIER
        )
        self.assertEqual(
            StructuralSchemaProfiler.infer_column_role("systolic_bp", "NUMERIC(5,2)"),
            ColumnSemanticRole.METRIC_QUANTITATIVE
        )
        self.assertEqual(
            StructuralSchemaProfiler.infer_column_role("admission_date", "TIMESTAMP"),
            ColumnSemanticRole.TEMPORAL
        )

        # Telecommunications
        self.assertEqual(
            StructuralSchemaProfiler.infer_column_role("msisdn", "VARCHAR(20)"),
            ColumnSemanticRole.ENTITY_IDENTIFIER
        )
        self.assertEqual(
            StructuralSchemaProfiler.infer_column_role("imsi", "VARCHAR(20)"),
            ColumnSemanticRole.ENTITY_IDENTIFIER
        )
        self.assertEqual(
            StructuralSchemaProfiler.infer_column_role("call_duration_seconds", "INTEGER"),
            ColumnSemanticRole.METRIC_QUANTITATIVE
        )
        self.assertEqual(
            StructuralSchemaProfiler.infer_column_role("call_status", "VARCHAR(10)", distinct_count=5),
            ColumnSemanticRole.CATEGORICAL
        )

        # Logistics & Defense
        self.assertEqual(
            StructuralSchemaProfiler.infer_column_role("waybill_number", "VARCHAR(40)"),
            ColumnSemanticRole.ENTITY_IDENTIFIER
        )
        self.assertEqual(
            StructuralSchemaProfiler.infer_column_role("tare_weight_kg", "FLOAT"),
            ColumnSemanticRole.METRIC_QUANTITATIVE
        )

    def test_document_source_profiler(self):
        """Verify cloud repository profiling for S3 and uploaded CSVs."""
        s3_paths = [
            "s3://defense-ops/munitions/inventory_report_2026.pdf",
            "s3://defense-ops/logistics/routes/africa_corridor.docx"
        ]
        s3_prof = DocumentSourceProfiler.profile_source(
            source_type="S3",
            source_name="defense-ops-s3",
            bucket_or_site_name="defense-ops",
            file_paths=s3_paths
        )
        self.assertEqual(s3_prof.source_type, "S3")
        self.assertIn("defense-ops", s3_prof.container_names)
        self.assertTrue(any(t in s3_prof.topic_tags for t in ["munitions", "logistics"]))
        self.assertIn("pdf", s3_prof.file_types)
        self.assertIn("docx", s3_prof.file_types)

        # Tabular upload profiling
        upload_prof = DocumentSourceProfiler.profile_uploaded_file(
            filename="subscribers_churn.csv",
            headers=["subscriber_id", "msisdn", "churn_score", "arpu", "region"]
        )
        self.assertTrue(upload_prof["is_tabular"])
        self.assertIn("subscriber_id", upload_prof["entity_keys"])
        self.assertIn("msisdn", upload_prof["entity_keys"])
        self.assertIn("churn_score", upload_prof["metric_keys"])

    def test_catalog_manager_l1_cache(self):
        """Verify CatalogManager in-memory registration and lookup."""
        org_id = f"test-org-{uuid.uuid4().hex[:8]}"
        telecom_profile = DatabaseProfile(
            job_id="job-telecom-01",
            database_name="telecom_cdr_db",
            domain_tags={"primary": ["cdr", "telecom", "calls", "subscribers"], "secondary": ["tower", "imsi"]},
            tables={
                "subscribers": TableProfile(
                    table_name="subscribers",
                    primary_entity_keys=["msisdn", "imsi"],
                    metric_keys=["arpu"]
                )
            },
            all_entity_keys=["msisdn", "imsi", "sim_serial"],
            all_metric_keys=["arpu", "call_duration_seconds"]
        )

        CatalogManager.register_database_profile_in_memory(org_id, telecom_profile)
        retrieved_catalog = CatalogManager.get_tenant_catalog(org_id)
        self.assertIn("telecom_cdr_db", retrieved_catalog.databases)

        db_prof = CatalogManager.get_database_profile(org_id, "telecom_cdr_db")
        self.assertIsNotNone(db_prof)
        self.assertEqual(db_prof.job_id, "job-telecom-01")

        all_entities = CatalogManager.get_all_entity_keys(org_id)
        self.assertIn("msisdn", all_entities)

        CatalogManager.evict(org_id)

    def test_universal_referential_pattern_fast_path(self):
        """Verify UNIVERSAL_REFERENTIAL_PATTERN correctly triggers on linguistic markers across verticals."""
        pattern = QueryCondenser.UNIVERSAL_REFERENTIAL_PATTERN

        # Universal pronouns and pointers
        self.assertTrue(bool(pattern.search("What about his latest test results?")))
        self.assertTrue(bool(pattern.search("Show her prescriptions")))
        self.assertTrue(bool(pattern.search("Check their call detail records")))
        self.assertTrue(bool(pattern.search("List its active waybills")))
        self.assertTrue(bool(pattern.search("What about that subscriber?")))
        self.assertTrue(bool(pattern.search("Show the same patient")))
        self.assertTrue(bool(pattern.search("Does he have any active symptoms?")))

        # Non-referential queries without pointer tokens must NOT match pattern
        self.assertFalse(bool(pattern.search("Calculate total network revenue for Q3")))
        self.assertFalse(bool(pattern.search("Where is branch Lagos Central located")))
        self.assertFalse(bool(pattern.search("Who founded Linux operating system")))

        # Fast-path bypass on first-turn queries with no prior history/memory
        empty_mem = SessionWorkingMemory(session_id="first-turn")
        self.assertFalse(QueryCondenser.should_condense("Calculate total network revenue for Q3", history=[], memory=empty_mem))
        self.assertFalse(QueryCondenser.should_condense("Where is branch Lagos Central located", history=[], memory=empty_mem))
        self.assertFalse(QueryCondenser.should_condense("ok, thank you", history=[{"role": "user", "content": "hi"}], memory=empty_mem))

    def test_dynamic_state_harvester_arbitrary_schema(self):
        """Verify DynamicStateHarvester extracts entities from non-banking SQL results."""
        memory = SessionWorkingMemory(session_id="session-telecom-01")
        
        telecom_rows = [
            {
                "msisdn": "08012345678",
                "imsi": "621200000001",
                "subscriber_name": "Ada Lovelace",
                "call_duration_seconds": 320,
                "arpu": 4500.50
            }
        ]

        catalog = TenantCatalog(
            org_id="org-telecom",
            databases={
                "telecom_cdr_db": DatabaseProfile(
                    job_id="job-cdr",
                    database_name="telecom_cdr_db",
                    all_entity_keys=["msisdn", "imsi"],
                    all_metric_keys=["call_duration_seconds", "arpu"]
                )
            }
        )

        updated_mem = StateHarvester.harvest(
            memory=memory,
            strategy_name="sql",
            query="Look up subscriber 08012345678",
            answer="Found subscriber Ada Lovelace with 320 seconds of call time.",
            sources=[],
            catalog=catalog,
            extra_meta={"rows": telecom_rows, "database_name": "telecom_cdr_db"}
        )

        self.assertEqual(updated_mem.scope, EntityScope.INDIVIDUAL.value)
        self.assertEqual(updated_mem.active_entities.get("msisdn"), "08012345678")
        self.assertEqual(updated_mem.active_entities.get("imsi"), "621200000001")
        self.assertIn("Ada Lovelace", updated_mem.active_names)
        self.assertIn("call_duration_seconds", updated_mem.active_metrics)
        self.assertIn("arpu", updated_mem.active_metrics)

    def test_dynamic_sql_strategy_routing_with_catalog(self):
        """Verify DynamicSQLStrategy._resolve_target_job routes using catalog domain tags."""
        job_telecom = IngestionJob(
            id="job-tel-1",
            org_id="org-123",
            name="telecom_cdr_db",
            source_type="POSTGRESQL",
            connection_config={}
        )
        job_billing = IngestionJob(
            id="job-bil-1",
            org_id="org-123",
            name="telecom_billing_db",
            source_type="POSTGRESQL",
            connection_config={}
        )

        strategy = DynamicSQLStrategy(vector_store=None, llm_service=None)

        catalog = TenantCatalog(
            org_id="org-123",
            databases={
                "telecom_cdr_db": DatabaseProfile(
                    job_id="job-tel-1",
                    database_name="telecom_cdr_db",
                    domain_tags={"primary": ["cdr", "call records", "dropped calls", "telemetry"], "secondary": ["tower"]},
                    tables={"call_records": TableProfile(table_name="call_records")}
                ),
                "telecom_billing_db": DatabaseProfile(
                    job_id="job-bil-1",
                    database_name="telecom_billing_db",
                    domain_tags={"primary": ["invoice", "subscription plan", "billing", "charges"], "secondary": ["rate"]},
                    tables={"invoices": TableProfile(table_name="invoices")}
                )
            }
        )
        CatalogManager.register_database_profile_in_memory("org-123", catalog.databases["telecom_cdr_db"])
        CatalogManager.register_database_profile_in_memory("org-123", catalog.databases["telecom_billing_db"])

        # Query 1: Call records inquiry -> Should route to telecom_cdr_db
        target_1 = strategy._resolve_target_job(
            query="Show dropped calls and call records for tower 402",
            db_jobs=[job_telecom, job_billing],
            schema_chunks=[],
            org_id="org-123"
        )
        self.assertEqual(target_1.name, "telecom_cdr_db")

        # Query 2: Invoice inquiry -> Should route to telecom_billing_db
        target_2 = strategy._resolve_target_job(
            query="What are the pending invoices and billing charges?",
            db_jobs=[job_telecom, job_billing],
            schema_chunks=[],
            org_id="org-123"
        )
        self.assertEqual(target_2.name, "telecom_billing_db")

        CatalogManager.evict("org-123")

    def test_query_planner_multi_source_and_vertical(self):
        """Verify QueryPlanner routes non-banking structured queries and cloud document queries."""
        # Tabular queries in telecom & healthcare
        plan_tel = QueryPlanner.analyze_and_plan("Calculate total data volume in call records per subscriber")
        self.assertTrue(plan_tel.is_structured_sql)

        plan_health = QueryPlanner.analyze_and_plan("What is the average systolic_bp of admitted patients?")
        self.assertTrue(plan_health.is_structured_sql)

        # Cloud Document Queries
        plan_doc = QueryPlanner.analyze_and_plan("What does the S3 safety protocol document say about chemical refinery leaks?")
        self.assertEqual(plan_doc.intent_category, "DOCUMENT_RAG")

        plan_sharepoint = QueryPlanner.analyze_and_plan("Review the clinical trial protocol on SharePoint")
        self.assertEqual(plan_sharepoint.intent_category, "DOCUMENT_RAG")

if __name__ == "__main__":
    unittest.main()
