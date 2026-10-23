# Retrieval Architecture Diagrams

> Mermaid diagrams for the multi-tenant RAG system retrieval layer.

---

## 1. High-Level Retrieval Architecture

```mermaid
graph TD
    subgraph Client["Client / HTTP"]
        R["POST /chat/query"]
    end

    subgraph Orch["UnifiedRAGOrchestrator\nexecute_unified_query_stream()"]
        CTX["Stage 2: Context Resolution\nSessionContextManager.get_memory()\nget_recent_history()"]
        COND["Stage 3: Query Condensation\nQueryCondenser.condense()"]
        PLAN["Stage 4: Query Planning\nQueryPlanner.analyze_and_plan()"]
        ROUTE["Stage 5: Strategy Router"]
        HARV["Stage 10: StateHarvester.harvest()\nSessionContextManager.save_memory()"]
    end

    subgraph Strategies["Retrieval Strategies"]
        CONV["ConversationalStrategy\nLLM-only"]
        META["SystemMetaStrategy\nDB Source Inventory"]
        SQL["DynamicSQLStrategy\nText-to-SQL"]
        SESS["SessionDocumentStrategy\nIn-Chat Documents"]
        ENT["EnterpriseKnowledgeStrategy\nQdrant + CrossEncoder + LLM"]
    end

    subgraph Ret["Retrieval"]
        FILT["RAGSecurityFilterBuilder\nACL + Tenant Filter"]
        HYB["HybridRetriever\nEmbed + HNSW Search"]
        RNK["CrossEncoderReranker\nms-marco-TinyBERT-L-2-v2"]
        GND["GroundingValidator\nContext + Citations"]
    end

    subgraph Store["Storage"]
        QD[("Qdrant\nHNSW in-RAM\nef=64")]
        PG[("PostgreSQL\nchat_messages\ningestion_jobs")]
        REDIS[("Redis\nsession_memory:*\nTTL=24h")]
    end

    R --> Orch
    CTX --> REDIS
    CTX --> PG
    COND --> CTX
    PLAN --> COND
    ROUTE --> PLAN

    ROUTE -->|"CONVERSATIONAL"| CONV
    ROUTE -->|"SYSTEM_META"| META
    ROUTE -->|"STRUCTURED_SQL"| SQL
    ROUTE -->|"session scope"| SESS
    ROUTE -->|"DOCUMENT_RAG"| ENT

    ENT --> FILT --> HYB --> QD
    HYB --> RNK --> GND
    SESS --> FILT
    SQL --> QD
    SQL --> PG
    META --> PG

    Strategies --> HARV
    HARV --> REDIS
```

---

## 2. Multi-Turn Retrieval Flow (Blackboard-Driven)

```mermaid
sequenceDiagram
    participant User
    participant API as FastAPI /chat/query
    participant DB as PostgreSQL\nchat_messages
    participant Redis as Redis\nsession_memory
    participant Cond as QueryCondenser
    participant Plan as QueryPlanner
    participant Orch as UnifiedRAGOrchestrator
    participant SQL as DynamicSQLStrategy
    participant ExtDB as External Database
    participant LLM as LLM
    participant Harv as StateHarvester

    Note over User,Harv: Turn 1 — First query (no prior context)

    User->>API: "Who is Adebayo Adekunle?"
    API->>DB: add_message(role=user)
    API->>Orch: execute_unified_query_stream()

    Orch->>Redis: get_memory(session_id) → empty blackboard
    Orch->>DB: get_recent_history() → []
    Orch->>Cond: should_condense() → False (no history, no entities)
    Note over Cond: LLM rewrite SKIPPED — 0ms overhead

    Orch->>Plan: analyze_and_plan("Who is Adebayo Adekunle?")
    Plan-->>Orch: QueryPlan{STRUCTURED_SQL}

    Orch->>SQL: DynamicSQLStrategy.execute_stream()
    SQL->>LLM: Generate SQL from DDL schema context
    LLM-->>SQL: SELECT * FROM customers WHERE first_name='Adebayo'...
    SQL->>ExtDB: execute_read_only_query()
    ExtDB-->>SQL: rows=[{customer_id:1008, bvn:'90000001008', ...}]
    SQL->>LLM: Synthesize NL answer from rows
    LLM-->>User: streaming tokens (SSE delta events)

    Orch->>Harv: harvest(memory, strategy=sql, rows=[...])
    Note over Harv: Extracts: customer_id=1008, bvn, name Adebayo Adekunle
    Harv->>Redis: save_memory({customer_id:1008, name:"Adebayo Adekunle"})
    API->>DB: add_message(role=assistant, answer, citations)

    Note over User,Harv: Turn 2 — Follow-up (elliptical, blackboard-driven)

    User->>API: "What are his account balances?"
    API->>Orch: execute_unified_query_stream()

    Orch->>Redis: get_memory() → {customer_id:1008, name:"Adebayo Adekunle", bvn}
    Orch->>DB: get_recent_history() → [T1 user, T1 assistant]
    Orch->>Cond: should_condense() → True (pronoun "his", has entities)
    Cond->>LLM: Rewrite with blackboard context + sanitized history
    LLM-->>Cond: "What are the account balances of Adebayo Adekunle (customer_id: 1008)?"

    Orch->>Plan: analyze_and_plan(rewritten_query)
    Plan-->>Orch: QueryPlan{STRUCTURED_SQL}
    Orch->>SQL: DynamicSQLStrategy — SQL on accounts table
    SQL->>LLM: Generate SQL with customer_id=1008
    SQL->>ExtDB: execute → rows=[{account_id, balance}]
    LLM-->>User: streaming tokens

    Orch->>Harv: harvest(memory, rows=[{account_id, balance}])
    Note over Harv: Adds account_id, balance="88450000.00" to blackboard
    Harv->>Redis: save_memory({...+account_id, balance, active_metrics})
```

---

## 3. Document Retrieval Flow (EnterpriseKnowledgeStrategy)

```mermaid
sequenceDiagram
    participant User
    participant Orch as UnifiedRAGOrchestrator
    participant Filt as RAGSecurityFilterBuilder
    participant Hyb as HybridRetriever
    participant VS as VectorStoreService Qdrant
    participant EmbLLM as LLM Embedding
    participant Rank as CrossEncoderReranker
    participant GV as GroundingValidator
    participant GenLLM as LLM Generation

    User->>Orch: "What is our expense reimbursement policy?"
    Note over Orch: QueryPlanner → DOCUMENT_RAG\nQueryCondenser → no rewrite (self-contained)

    Orch->>Filt: build_search_filter(org_id, dept_id, user_id, role=MEMBER)
    Filt-->>Orch: Filter{must:[org_id], must_not:[scope=session], should:[PUBLIC, uploader, dept-DEPARTMENT]}

    Orch->>Hyb: retrieve(query, filter, limit=15, threshold=0.35)
    Hyb->>EmbLLM: get_embeddings("What is our expense reimbursement policy?")
    EmbLLM-->>Hyb: query_vector [1024 floats]
    Hyb->>VS: search_vectors(vector, filter, limit=15, ef=64)
    VS-->>Hyb: 15 hits sorted by cosine score
    Hyb-->>Orch: 15 candidate chunk dicts

    Orch->>Rank: rerank(query, candidates, top_n=3)
    Rank->>Rank: CrossEncoder.predict(15 query-passage pairs)
    Rank-->>Orch: top 3 chunks by cross-encoder relevance

    Orch->>GV: format_grounded_context(top3)
    GV-->>Orch: labeled context_block + raw_sources

    Orch->>GV: deduplicate_sources(raw_sources)
    GV-->>Orch: deduplicated sources (1 per document, max relevance, chunk_count)

    Orch->>GenLLM: stream_text(XML-delimited prompt, system=STRICT)
    loop Token streaming
        GenLLM-->>Orch: token
        Orch-->>User: SSE delta event {content: token}
    end

    GV->>GV: validate_grounding(answer, sources)
    Orch-->>User: SSE metadata {sources, is_grounded=True, confidence=0.89, latency_ms}
```

---

## 4. Database Retrieval Flow (Text-to-SQL with Self-Correction)

```mermaid
sequenceDiagram
    participant User
    participant SQL as DynamicSQLStrategy
    participant QD as Qdrant Schema DDL
    participant Agent as DynamicSQLAgent
    participant LLM as LLM SQL Generation
    participant Guard as SQLSecurityGuard AST
    participant ExtDB as External Database
    participant GenLLM as LLM Synthesis

    User->>SQL: "How many active loans are above 10M?"
    Note over SQL: QueryPlanner → STRUCTURED_SQL

    SQL->>QD: scroll DDL schema chunks\n[org_id, source_type=POSTGRES_DB]
    QD-->>SQL: DDL context (tables, columns, FK, sample values)

    SQL->>Agent: generate_and_execute_sql(\n  user_query, schema_ctx, dialect=postgresql, url\n)

    rect rgb(30,50,80)
        Note over Agent,ExtDB: Attempt 1
        Agent->>LLM: SQL generation prompt\n[schema context + user query]
        LLM-->>Agent: SELECT COUNT(*) FROM loans WHERE status='ACTIVE' AND amount>10000000

        Agent->>Guard: validate_query(sql) — AST parse
        alt SELECT statement — passes
            Guard->>ExtDB: SET TRANSACTION READ ONLY
            Guard->>ExtDB: execute(sql, timeout=15000ms)
            ExtDB-->>Agent: rows=[{count:47}] → SUCCESS
        else Non-SELECT blocked
            Guard-->>Agent: InsecureQueryError → security_violation
        end
    end

    rect rgb(60,30,30)
        Note over Agent,ExtDB: Attempt 2 (on error)
        Agent->>LLM: Retry prompt\n[previous SQL + error diagnostic]
        LLM-->>Agent: corrected SQL
        Agent->>Guard: validate again
        Guard->>ExtDB: execute
        ExtDB-->>Agent: rows → SUCCESS
    end

    Agent-->>SQL: {status:success, sql, rows, row_count, columns}

    SQL->>GenLLM: stream_text(synthesis_prompt:\n  query + executed_sql + 47 rows)
    loop Token streaming
        GenLLM-->>SQL: token
        SQL-->>User: SSE delta event
    end

    SQL-->>User: SSE metadata {source: {sql_query, row_count:47, db_name}}
```

---

## 5. Blackboard Lifecycle (SessionWorkingMemory)

```mermaid
stateDiagram-v2
    [*] --> Empty : get_memory() new session

    Empty --> INDIVIDUAL : Turn 1 — single customer resolved\ncustomer_id + bvn bound

    INDIVIDUAL --> INDIVIDUAL : Follow-up on same customer\nprimary_anchor_id protects binding

    INDIVIDUAL --> COLLECTION : Multi-customer result\nlen(distinct_customers) > 1

    INDIVIDUAL --> AGGREGATE : Topic shift — apart from X, other industries\nevict_for_topic_shift — entity IDs cleared

    INDIVIDUAL --> SYSTEM_META : System architecture query\nfull eviction — zero entity injection

    COLLECTION --> INDIVIDUAL : Single-customer scoped query

    AGGREGATE --> INDIVIDUAL : New individual lookup

    state INDIVIDUAL {
        active_entities: customer_id bvn account_id loan_id
        active_names: first_name last_name
        active_metrics: balance interest_rate
        primary_anchor_id: protects against multi-row drift
    }

    state AGGREGATE {
        active_vendors: MTN Slot NG
        active_documents: policy.pdf
        Entity IDs cleared
    }

    state SYSTEM_META {
        All entities cleared
        No context injected into query rewriting
    }
```
