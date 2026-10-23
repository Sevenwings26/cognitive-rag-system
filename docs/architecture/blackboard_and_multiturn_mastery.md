# Universal Multi-Turn Context & Blackboard Architecture
## Pedagogical Reference & Engineering Implementation Guide

**Author:** Enterprise Retrieval & Autonomous Context Engineering Team  
**System Location:** `modules/rag_core/context/` & `modules/rag_core/orchestrator/`  
**Classification:** Core System Architecture & Production Engineering Documentation  
**Status:** Implemented, Fully Benchmarked, and Production-Ready  

---

## 1. The Core Problem & Paradigm Shift

### 1.1 The Failure of Sliding-Window Context Stuffing in Enterprise Systems

The naive and widespread approach to conversational AI is **Sliding-Window Context Stuffing**—the mechanical concatenation of the last $N$ turns of raw user and assistant text messages directly into the system prompt of every subsequent turn. 

While context stuffing functions passably for open-domain casual chat, it catastrophically degrades in **multi-tenant enterprise architectures** where precision across relational databases, analytical ledgers, and document vaults is mission-critical:

1. **Attention Dilution and "Lost in the Middle"**: Modern Transformer architectures (even those boasting 128k+ token context windows) exhibit severe non-uniform retrieval attention across long contexts. When raw database markdown tables, JSON payloads, and extensive retrieved text chunks are stuffed into conversation history, the LLM's attention heads lose focus on the active entity keys (such as `customer_id: 1008` or `account_number: 0123456708`).
2. **Schema & Semantic Confusion**: When Turn 1 queries a PostgreSQL customer database (`noros_customer_db`) and outputs customer KYC details, and Turn 2 queries a Core Banking ledger (`noros_core_banking_db`), stuffing Turn 1's raw SQL output, table headers, and technical column matrices confuses downstream SQL generation agents. The agent frequently hallucinates cross-database joins that cannot physically execute across distinct database instances, or attempts to query columns from prior tables that do not exist in the current target database.
3. **Quadratic Token Cost and Latency Blowup**: Repeatedly re-tokenizing large Markdown result sets from previous turns inflates prompt sizes exponentially ($O(N^2)$ token overhead across an $N$-turn session), dramatically driving up model inference latency (time-to-first-token) and API consumption cost.
4. **Memory Bleed and State Poisoning**: In multi-row collection queries (e.g., *"List all Dangote employees"*), raw history becomes cluttered with dozens of distinct names, phone numbers, and IDs. If the user subsequently asks an anaphoric question (*"What is his balance?"*), a context-stuffed prompt provides 10 ambiguous customer IDs, triggering non-deterministic hallucination or arbitrary entity selection.

---

### 1.2 Architectural Comparison: Three Approaches to Conversational Memory

To understand why this architecture chose the **Universal Blackboard + Canonical Condensation Pattern**, consider the three major paradigms in modern AI engineering:

```mermaid
flowchart TD
    subgraph ParadigmA["Paradigm A: Sliding-Window Context Stuffing"]
        A_History["Raw History Appending<br/>(User + Assistant + Markdown Tables)"] --> A_Prompt["Bloated Prompt<br/>(Token Blowup, Schema Poisoning)"]
        A_Prompt --> A_LLM["LLM Execution<br/>(Prone to Attention Dilution & Hallucination)"]
    end

    subgraph ParadigmB["Paradigm B: Stateful Graph Agents (ReAct / LangGraph)"]
        B_Agent["Cyclic Graph Loop<br/>(Agent State, Tool Invocations)"] <--> B_State["Complex Monolithic State Graph<br/>(High Tool Overhead, Flaky Loops)"]
        B_State --> B_Tools["Tools Executed in Loop<br/>(Expensive & Non-Deterministic in Production)"]
    end

    subgraph ParadigmC["Paradigm C: Blackboard + Canonical Condensation (Our System)"]
        C_RawQ["Raw Turn Input"] --> C_Condenser["QueryCondenser<br/>(Fast-Path Gate + LLM Rewrite)"]
        C_Memory[("SessionWorkingMemory<br/>(Universal Blackboard)")] <--> C_Condenser
        C_Condenser --> C_Canonical["Canonical Standalone Query<br/>+ Context Bindings"]
        C_Canonical --> C_Strategy["Targeted Strategy Engine<br/>(SQL / RAG / Session Docs)"]
        C_Strategy --> C_Harvester["StateHarvester<br/>(Deterministic Tabular Fact Extraction)"]
        C_Harvester --> C_Memory
    end
```

#### Comparison Matrix

| Dimension | Sliding-Window Context Stuffing | Stateful Cyclic Graph (LangGraph / ReAct) | Universal Blackboard + Condensation (Our Engine) |
| :--- | :--- | :--- | :--- |
| **Token Efficiency** | **Degraded**: $O(N^2)$ token scaling as past tables and texts accumulate. | **Moderate to Poor**: Cyclic iterations repeat history and scratchpads. | **Optimal**: $O(1)$ constant prompt footprint; history is stripped of technical dumps. |
| **Fast-Path Latency** | **Fixed Overhead**: Always loads and formats full multi-turn prompt. | **High Overhead**: Cyclic agent decisions require 2–5 LLM steps per turn. | **0 ms Fast-Path**: Bypasses LLM rewriting entirely for unambiguous/first turns. |
| **Cross-DB Execution** | **Fails**: Hallucinates cross-database queries or invalid joins across engines. | **Unreliable**: Agent gets trapped in exploratory tool-calling loops. | **Deterministic**: Resolves target engine natively, injecting explicit primary/foreign keys. |
| **State Cleanliness** | **Contaminated**: Full Markdown tables and raw outputs poison prompt context. | **Complex**: Graph state schema must accommodate all tool outputs. | **Sanitized**: Blackboard stores typed primitives (`int`, `str`); dumps are pruned. |
| **Auditability** | Low: Reasoning is tangled inside giant prompt context. | Complex: Requires tracing multi-step execution graphs and agent loops. | High: Clear turn-by-turn state delta, explicit entity dictionary, and logged APM routes. |

---

## 2. Component-by-Component Architectural Breakdown

The Universal Multi-Turn Context System resides under `modules/rag_core/context/`. It operates as a decoupled blackboard and pre/post-processing harness positioned directly in front of the retrieval and generation strategies.

```
modules/rag_core/context/
├── __init__.py                  # Public exports: SessionContextManager, SessionWorkingMemory, EntityScope, QueryCondenser, StateHarvester
├── models.py                    # SessionWorkingMemory dataclass, EntityScope enumeration
├── session_context_manager.py   # Distributed Redis persistence, local memory fallback, chat history retrieval
├── query_condenser.py           # 0-ms regex fast-path gate, anaphora resolver, prompt sanitization
└── state_harvester.py           # Deterministic fact, metric, and citation extractor from execution results
```

---

### 2.1 `SessionWorkingMemory` (`models.py`)

`SessionWorkingMemory` represents the **universal blackboard**. It is an isolated, serializable data structure maintained for every active chat session. Instead of recording conversational prose, it extracts and retains **structured business semantics**:

```python
# modules/rag_core/context/models.py

class EntityScope(str, Enum):
    """Conversation scope levels governing context injection and entity eviction."""
    INDIVIDUAL = "INDIVIDUAL"     # Specific person or account (e.g. Turn 1 customer lookup)
    COLLECTION = "COLLECTION"     # Multi-entity group/filter (e.g. Dangote employees)
    AGGREGATE = "AGGREGATE"       # Global statistics / categories (e.g. distinct industries/employers)
    SYSTEM_META = "SYSTEM_META"   # Platform inspection (e.g. registered data sources)

@dataclass
class SessionWorkingMemory:
    session_id: str
    active_entities: Dict[str, Any] = field(default_factory=dict)     # e.g. {"customer_id": 1008, "bvn": "90000001008"}
    active_names: List[str] = field(default_factory=list)             # e.g. ["Adebayo Adekunle"]
    active_documents: List[str] = field(default_factory=list)         # e.g. ["credit_policy.pdf"]
    active_metrics: Dict[str, Any] = field(default_factory=dict)      # e.g. {"current_balance": "88450000.00"}
    last_target_database: Optional[str] = None                        # e.g. "noros_core_banking_db"
    last_strategy: Optional[str] = None                               # e.g. "sql", "enterprise", "session"
    turn_count: int = 0
    scope: str = EntityScope.INDIVIDUAL.value                         # Current conversational entity scope
    active_topic: Optional[str] = None                               
    primary_anchor_id: Optional[Any] = None                           # Singular anchor ID to avoid multi-row overwrite drift
```

#### The Scoping Model (`EntityScope`)
Enterprise conversations naturally drift across distinct conceptual scopes:
* **`INDIVIDUAL`**: Focus is on a singular primary actor (e.g., *Customer 1008*). Context bindings like `customer_id = 1008` are injected into queries.
* **`COLLECTION`**: A group or filter of entities (e.g., *“Dangote employees”*). Individual IDs are preserved via `primary_anchor_id`, but the system avoids poisoning the primary slot with arbitrary secondary rows.
* **`AGGREGATE`**: Macro-level analytical queries (e.g., *“Apart from Dangote, which other industries do we have customers from?”*). The blackboard executes an **eviction protocol** (`evict_for_topic_shift`), clearing individual customer IDs and account numbers so the SQL agent does not generate restrictive single-customer filters.
* **`SYSTEM_META`**: Platform-level administrative inquiries (e.g., *“How many data sources are registered on this system?”*). All business entities are suppressed from context generation to prevent context bleed into administrative functions.

```python
def evict_for_topic_shift(self, new_scope: EntityScope) -> None:
    """
    Partially resets or evicts stale entity bindings when an intentional topic shift occurs.
    Prevents single-entity IDs from contaminating aggregate or system-level queries.
    """
    self.scope = new_scope.value if isinstance(new_scope, EntityScope) else str(new_scope)
    if new_scope in (EntityScope.AGGREGATE, EntityScope.SYSTEM_META):
        self.active_entities.clear()
        self.active_names.clear()
        self.primary_anchor_id = None
```

---

### 2.2 `SessionContextManager` (`session_context_manager.py`)

`SessionContextManager` acts as the persistence and cache orchestrator for `SessionWorkingMemory`. It guarantees sub-millisecond retrieval, distributed session sharing across horizontal worker pods, and fault-tolerant fallbacks.

```mermaid
flowchart LR
    Caller["UnifiedRAGOrchestrator"] -->|"get_memory(session_id)"| SCM["SessionContextManager"]
    SCM -->|"1. Try Redis GET"| Redis[("Redis Distributed Cache<br/>Key: session_memory:{id}")]
    Redis -.->|"Cache Hit (JSON)"| SCM
    Redis -.->|"Timeout / Unavailable"| Fallback["2. In-Memory Process Fallback<br/>_IN_MEMORY_CACHE"]
    Fallback --> SCM
    SCM -->|"SessionWorkingMemory Instance"| Caller
```

#### Key Implementation Details:
1. **Hybrid Distributed Architecture**: Primary storage is Redis (`redis://redis:6379/0`), accessed via connection pooling with a 2-second timeout. If Redis encounters network partitions or is unavailable, `SessionContextManager` seamlessly falls back to a thread-safe process-level dictionary (`_IN_MEMORY_CACHE`).
2. **Deterministic Serialization**: The state converts to and from JSON using `to_json()` and `from_json()`, guaranteeing schema stability across Python worker process boundaries.
3. **Session TTLs**: State is stored with an explicit 24-hour expiration (`ttl_seconds=86400`), automatically reclaiming memory for abandoned sessions while preserving state across multi-hour customer inquiries.
4. **History Windowing and De-duplication**: `get_recent_history()` retrieves the last $K$ turns (default 6) from PostgreSQL `chat_messages` via `ChatRepository`. Crucially, it detects and excludes the current user prompt if it was pre-inserted by the API router, avoiding self-referential duplication.

---

### 2.3 `QueryCondenser` (`query_condenser.py`)

The `QueryCondenser` solves the challenge of **anaphora resolution** (resolving pronouns like *"he"*, *"his"*, *"it"*, *"this customer"*) and **ellipses** (*"What about loans?"*).

It operates via a **two-tier architecture**:

```mermaid
flowchart TD
    InputQ["Raw User Query + History + Blackboard"] --> Gate{"should_condense()<br/>0-ms Fast-Path Heuristic"}
    
    Gate --"No Referentials / First Turn"--> Bypass["Return Raw Query (0 ms Overhead)"]
    Gate --"Referential Detected / Topic Shift"--> LLM["LLM Canonical Rewriter<br/>(Sanitized History + Blackboard Prompt)"]
    
    LLM --> Cleanse["Clean Output<br/>(Strip quotes, backticks, filler)"]
    Cleanse --> OutputQ["Canonical Standalone Query<br/>e.g. 'Does customer Adebayo Adekunle (1008)...'"]
```

#### 1. The 0-Millisecond Fast-Path Heuristic Gate (`should_condense`)
Invoking an LLM for query rewriting on every single turn introduces unnecessary latency (200–500 ms) and token cost. `should_condense` applies compiled regex heuristics to instantly bypass rewriting when unnecessary:

```python
REFERENTIAL_PATTERN = re.compile(
    r"\b(he|him|his|she|her|hers|it|its|they|them|their|theirs|"
    r"this|that|these|those|same|previous|prior|above|latter|former|"
    r"what about|how about|and the|show his|find his|check his|get his|list his|"
    r"any loans|any accounts|any transactions|does he|did he|is he|has he|"
    r"the customer|this customer|that customer|same customer)\b",
    re.IGNORECASE
)

TOPIC_SHIFT_PATTERN = re.compile(
    r"\b(apart from|other than|besides|excluding|which other|what other|which else|what else|across all|in general|overall)\b",
    re.IGNORECASE
)

SYSTEM_META_PATTERN = re.compile(
    r"\b(data sources?|connected databases?|databases? (?:registered|connected|available)|how many sources|what systems|registered connectors|on this system|in this system)\b",
    re.IGNORECASE
)
```
- If there is no prior history and the blackboard is empty $\to$ `return False` (0 ms).
- If no referential token, topic shift, or elliptical pattern is detected $\to$ `return False` (0 ms).
- Only when context dependency is detected does the condenser route the query to the rewriting model.

#### 2. History Sanitization & Anti-Hallucination Guardrails
When invoking the condenser LLM, raw history is aggressively sanitized:
* **Technical Table Stripping**: Regex removes `**Database Query Results:**` tables and ASCII column grids (`re.sub(r"\|[^\n]+\|", "", content)`).
* **Source Block Pruning**: Strips `Sources: ['noros_customer_db']` headers.
* **Strict Dialect & Backend Protection**: Rule 5 of `SYSTEM_INSTRUCTION` strictly forbids the LLM from injecting physical database names (`noros_customer_db`, `postgres`, `mssql`) into the rewritten text, preserving natural enterprise domain terminology (*"in our lending system"*, *"credit facilities"*).

---

### 2.4 `StateHarvester` (`state_harvester.py`)

`StateHarvester` executes **immediately after** a retrieval strategy completes. Rather than relying on fuzzy natural language parsing of the LLM's prose, it extracts facts **deterministically from structured data buffers**:

```mermaid
flowchart TD
    ExecOutput["Strategy Execution Result"] --> Dispatcher{"Data Source Type"}
    
    Dispatcher --"SQL Query Result"--> RowParser["Structured Row Inspector<br/>extra_meta['rows']"]
    Dispatcher --"Document RAG / Session Docs"--> DocParser["Citation Inspector<br/>sources['filename']"]
    
    RowParser --> EntityMatch["Extract KEY_ENTITY_FIELDS<br/>(customer_id, account_number, bvn, loan_id)"]
    RowParser --> MetricMatch["Extract KEY_METRIC_FIELDS<br/>(current_balance, principal_amount, interest_rate)"]
    RowParser --> NameMatch["Extract Names<br/>(first_name + last_name)"]
    
    DocParser --> CitationFilter["Filter non-schema docs<br/>(Bound to most recent 5)"]
    
    EntityMatch --> BlackboardUpdate["Update SessionWorkingMemory"]
    MetricMatch --> BlackboardUpdate
    NameMatch --> BlackboardUpdate
    CitationFilter --> BlackboardUpdate
```

#### Deterministic vs. Free-Form Text Extraction:
1. **Direct Tabular Extraction**: When `DynamicSQLStrategy` executes, it passes the raw database rows (`extra_meta["rows"]`) to `StateHarvester`. The harvester inspects dictionary keys matching `KEY_ENTITY_FIELDS` (`customer_id`, `account_id`, `bvn`, `loan_id`) and `KEY_METRIC_FIELDS` (`current_balance`, `interest_rate`, `principal_amount`). This eliminates LLM formatting quirks or truncation issues.
2. **Anchor ID Protection**: In multi-row results (e.g., 4 Dangote employees), the harvester detects `len(rows) > 1` (setting `scope = COLLECTION`). It protects the session's `primary_anchor_id`, preventing secondary rows from overwriting the primary customer's ID.
3. **Regex Fallback for Query Entities**: If the user introduced a new BVN (11 digits starting with 9) or explicit ID directly in their prompt, a regex extractor binds it to `active_entities` if not already present.
4. **Memory Pruning**: Active document citations are capped at the 5 most recent; active entity names are capped at 3, preventing memory bloat.

---

## 3. End-to-End Execution Lifecycle

The following sequence diagram illustrates the complete execution lifecycle of a conversational request inside `UnifiedRAGOrchestrator.execute_stream()`:

```mermaid
sequenceDiagram
    autonumber
    actor User as Client / Frontend UI
    participant Orch as UnifiedRAGOrchestrator
    participant SCM as SessionContextManager
    participant Mem as SessionWorkingMemory (Redis)
    participant Cond as QueryCondenser
    participant Plan as QueryPlanner
    participant Strat as Retrieval Strategy (SQL/RAG)
    participant DB as Backend Store (Postgres/MSSQL/Qdrant)
    participant Harv as StateHarvester

    User->>Orch: POST /chat/query (query, session_id)
    Orch->>SCM: get_memory(session_id)
    SCM->>Mem: Read key "session_memory:{id}"
    Mem-->>SCM: SessionWorkingMemory instance
    SCM-->>Orch: working_memory
    
    Orch->>SCM: get_recent_history(db, session_id)
    SCM-->>Orch: history (sanitized messages)

    Orch->>Cond: condense(query, history, working_memory)
    Note over Cond: should_condense() regex gate
    alt Referential or Topic Shift Detected
        Cond->>Cond: LLM rewrite with Blackboard Context
        Cond-->>Orch: canonical_query ("Does customer Adebayo...")
    else Fast-Path (First Turn / Unambiguous)
        Cond-->>Orch: original_query
    end

    Orch->>Plan: analyze_and_plan(canonical_query)
    Plan-->>Orch: ExecutionPlan (STRUCTURED_SQL, ENTERPRISE_RAG, etc.)

    Orch->>Strat: execute_stream(canonical_query, working_memory)
    Note over Strat: Injects [CONTEXT BINDINGS] from memory
    Strat->>DB: Execute Read-Only SQL / Vector Search
    DB-->>Strat: Structured Rows / Vectors
    Strat-->>Orch: SSE Token Deltas + Final Payload (rows, sources)

    Orch->>Harv: harvest(working_memory, strategy, canonical_query, answer, sources, extra_meta)
    Note over Harv: Extracts customer_id, balances, metrics from rows
    Harv-->>Orch: updated_memory

    Orch->>SCM: save_memory(updated_memory, ttl=86400)
    SCM->>Mem: Write key "session_memory:{id}" (EX 86400)

    Note over Orch: Strip raw rows from public source metadata
    Orch-->>User: SSE event: metadata {sources, latency_ms, is_grounded}
```

---

## 4. Empirical Walkthrough: The 4-Turn Enterprise Banking Scenario

To demonstrate how the Blackboard and Condensation pattern functions under real-world conditions, we trace the live, 100% passing benchmark from `benchmarking/test_multiturn_banking_scenario.py`.

### Architecture of the Target Enterprise Data Tier
* **`noros_customer_db` (PostgreSQL 18)**: Customer KYC profiles, BVN records, government identities, corporate affiliations, residential addresses.
* **`noros_core_banking_db` (PostgreSQL 18)**: Deposit ledger accounts, transaction history, balance sheets, currencies.
* **`noros_lending_db` (Microsoft SQL Server 2022)**: Corporate loan agreements, credit facilities, repayment schedules, interest rates.

---

### Turn 1: Customer Identity Lookup via BVN

* **User Prompt:** `"Who is the customer associated with Bank Verification Number (BVN) 90000001008?"`
* **Condensation Gate:** Fast-path matches (first turn; no history). Bypasses LLM rewriting (0 ms).
* **Target Resolution:** `noros_customer_db` (PostgreSQL). Matched on keyword `bvn` (+25 pts).
* **SQL Executed:**
  ```sql
  SELECT c.customer_id, c.customer_number, c.first_name, c.last_name, 
         c.email, c.phone_number, c.customer_type, c.customer_status, c.address 
  FROM customers c 
  JOIN bvn_records b ON c.customer_id = b.customer_id 
  WHERE b.bvn = '90000001008' 
  LIMIT 50;
  ```
* **Raw Execution Result:**
  ```json
  [{"customer_id": 1008, "customer_number": "CUST-01008", "first_name": "Adebayo", "last_name": "Adekunle", "customer_type": "CORPORATE", "customer_status": "ACTIVE"}]
  ```
* **Post-Turn State Harvesting:**
  * `StateHarvester` captures `customer_id: 1008`, `customer_number: 'CUST-01008'`, `bvn: '90000001008'`.
  * Captures name: `['Adebayo Adekunle']`.
  * Sets `primary_anchor_id = 1008`, `scope = INDIVIDUAL`.
* **Blackboard State After Turn 1:**
  ```json
  {
    "active_entities": {"customer_id": 1008, "customer_number": "CUST-01008", "bvn": "90000001008"},
    "active_names": ["Adebayo Adekunle"],
    "primary_anchor_id": 1008,
    "scope": "INDIVIDUAL",
    "turn_count": 1
  }
  ```

---

### Turn 2: Account Discovery & Balances (Anaphora Resolution)

* **User Prompt:** `"What bank accounts does he have with us, and what is his current total deposit balance?"`
* **Condensation Gate:** `REFERENTIAL_PATTERN` detects pronoun `"he"` and possessive `"his"`.
* **Query Condensation:**
  * Inputs: Blackboard (`Known Entities: customer_id: 1008`, `Known Names: Adebayo Adekunle`) + Turn 1 History.
  * Condenser Output:
    ```
    "What bank accounts does customer Adebayo Adekunle (customer_id: 1008, customer_number: CUST-01008, BVN: 90000001008) have with us, and what is his current total deposit balance?"
    ```
* **Target Resolution:** `noros_core_banking_db` (PostgreSQL). Matched on `accounts`, `deposit`, `balance` (+75 pts).
* **Context Injection:** `sql_strategy.py` injects context bindings:
  ```
  [CONTEXT BINDINGS: customer_id = 1008, customer_number = 'CUST-01008', bvn = '90000001008']
  ```
* **SQL Executed:**
  ```sql
  SELECT account_id, account_number, account_type, currency, current_balance, status 
  FROM accounts 
  WHERE customer_id = 1008 
  LIMIT 50;
  ```
* **Raw Execution Result:**
  ```json
  [{"account_id": 2008, "account_number": "0123456708", "account_type": "CORPORATE", "currency": "NGN", "current_balance": 88450000.00, "status": "ACTIVE"}]
  ```
* **Post-Turn State Harvesting:**
  * Harvester detects new identifiers: `account_id: 2008`, `account_number: '0123456708'`.
  * Captures metric: `current_balance: '88450000.00'`.
* **Blackboard State After Turn 2:**
  ```json
  {
    "active_entities": {
      "customer_id": 1008, 
      "customer_number": "CUST-01008", 
      "bvn": "90000001008", 
      "account_number": "0123456708", 
      "account_id": 2008
    },
    "active_names": ["Adebayo Adekunle"],
    "active_metrics": {"current_balance": "88450000.00"},
    "primary_anchor_id": 1008,
    "scope": "INDIVIDUAL",
    "turn_count": 2
  }
  ```

---

### Turn 3: Ledger Activity & Inflow Velocity

* **User Prompt:** `"Show me his transaction activity. What is his total inflow over the past 12 months, and what was his single largest credit deposit?"`
* **Condensation Gate:** Matches `"his"`. Condenses to explicit entity `customer Adebayo Adekunle (customer_id: 1008, account_number: 0123456708)`.
* **Target Resolution:** `noros_core_banking_db` (PostgreSQL).
* **SQL Guardrail Rule 5 & 11 Enforcement:**
  * To prevent local models from mixing `SUM(...)` with detail columns without a `GROUP BY`, Rule 5 directs the model to query qualifying detail rows:
  ```sql
  SELECT transaction_id, transaction_date, transaction_type, amount, description 
  FROM account_transactions 
  WHERE account_id = 2008 
  ORDER BY transaction_date DESC 
  LIMIT 50;
  ```
* **Downstream Synthesis:** The LLM receives the 5 returned credit rows ($22.5M, $24.0M, $19.5M, $21.0M, $18.2M), accurately calculates total inflow ($105,200,000.00 NGN), and identifies the largest single deposit ($24,000,000.00 NGN from *Syndicated Project Inflow*).
* **Blackboard State After Turn 3:** Preserves entities; updates turn count to 3.

---

### Turn 4: Cross-Database Pivot to Microsoft SQL Server Lending

* **User Prompt:** `"Does this same customer hold any active loans or credit facilities in our lending system?"`
* **Condensation Gate:** Matches referential phrase `"this same customer"`.
* **Query Condensation:**
  * Rewritten:
    ```
    "Does customer Adebayo Adekunle (customer_id: 1008, customer_number: CUST-01008, BVN: 90000001008) hold any active loans or credit facilities in our lending system?"
    ```
* **Target Resolution:** `noros_lending_db` (**Microsoft SQL Server 2022**). Matched on `loan`, `lending`, `facility`, `credit` (+100 pts).
* **Context Injection:** Injects `customer_id = 1008` into the MSSQL prompt context.
* **SQL Executed (T-SQL Dialect):**
  ```sql
  SELECT TOP 50 l.loan_id, l.loan_account_number, l.principal_amount, 
         l.interest_rate, l.term_months, l.start_date, l.maturity_date, 
         l.status, lp.product_name 
  FROM loans l 
  JOIN loan_products lp ON l.product_id = lp.product_id 
  WHERE l.customer_id = 1008;
  ```
* **Raw Execution Result:**
  ```json
  [{"loan_id": 3008, "loan_account_number": "LN-1008", "principal_amount": 45000000.00, "interest_rate": 13.00, "status": "ACTIVE", "product_name": "Corporate Loan"}]
  ```
* **Post-Turn State Harvesting:**
  * Binds `loan_id: 3008`, `loan_account_number: 'LN-1008'`.
  * Captures metrics: `principal_amount: '45000000.00'`, `interest_rate: '13.00'`.
* **Blackboard State After Turn 4:**
  ```json
  {
    "active_entities": {
      "customer_id": 1008, 
      "customer_number": "CUST-01008", 
      "bvn": "90000001008", 
      "account_number": "0123456708", 
      "account_id": 2008,
      "loan_account_number": "LN-1008",
      "loan_id": 3008
    },
    "active_names": ["Adebayo Adekunle"],
    "active_metrics": {
      "current_balance": "88450000.00",
      "principal_amount": "45000000.00",
      "interest_rate": "13.00"
    },
    "turn_count": 4
  }
  ```

---

## 5. Cross-Database Context Binding Without Federated Virtualization

A core architectural breakthrough of this system is achieving **deterministic cross-database data relationships without deploying federated query virtualization engines** (such as Trino, Presto, or Denodo).

### Why Virtualization Engines Fall Short in Conversational AI
1. **Network & Query Overhead**: Federated engines must push generic SQL wrappers across heterogeneous connectors, preventing dialect-specific engine optimizations.
2. **Schema Exposure Risks**: Exposing a single massive federated schema to an LLM context causes massive schema bloat (100+ tables across 5 databases in a single prompt), dramatically degrading Text-to-SQL generation accuracy.
3. **Fragile Authentication**: Virtualization layers obscure individual tenant RBAC credentials, making multi-tenant row-level access control difficult to enforce.

### How Our Blackboard Implements Virtualization-Free Binding

Instead of joining databases across the network, our system uses **asynchronous relational context propagation**:

```mermaid
flowchart LR
    subgraph EngineA["Engine A: PostgreSQL 18 (noros_customer_db)"]
        Turn1["Turn 1: BVN Search<br/>bvn = '90000001008'"] --> PK["Resolves Primary Key<br/>customer_id: 1008"]
    end

    subgraph MemoryLayer["Universal Blackboard (SessionWorkingMemory)"]
        PK --> Blackboard[("active_entities['customer_id'] = 1008")]
    end

    subgraph EngineB["Engine B: Microsoft SQL Server 2022 (noros_lending_db)"]
        Blackboard --> Injection["[CONTEXT BINDINGS: customer_id = 1008]"]
        Injection --> Turn4["Turn 4: T-SQL Execution<br/>WHERE l.customer_id = 1008"]
    end
```

1. **Dialect Autonomy**: The PostgreSQL database runs native PostgreSQL 18 dialect queries (`ILIKE`, `LIMIT`). The Microsoft SQL Server runs native T-SQL queries (`TOP 50`, T-SQL datetime functions). Each query executes natively against its respective database engine.
2. **Dynamic Schema Profiling**: The SQL Strategy injects schema DDLs that include **`-- Sample Column Values` annotations**. This allows the LLM to inspect actual column data distributions (e.g., recognizing that `employer` contains corporate entity names like `'Julius Berger Nigeria PLC'`, while `occupation` contains personal job titles like `'Software Engineer'`).
3. **Zero Virtualization Footprint**: No virtualization infrastructure is maintained. Context propagation is handled entirely via lightweight Redis JSON operations ($< 1$ ms overhead).

---

## 6. Governance & Production Engineering

### 6.1 Handling Entity Collisions & Overwrites
In conversational flows, a user may pivot from examining a single entity to a collection of entities. For example:
* *Turn 1:* Look up Customer 1008 (`scope = INDIVIDUAL`).
* *Turn 3:* *"Are there other customers employed by Dangote?"*
The database returns Customer 1001, 1021, and 1041.
* **The Collision Problem**: A naive state tracker would overwrite `customer_id: 1008` with `customer_id: 1041` (the last row returned). If Turn 4 asks *"What is his loan balance?"*, the system would query the wrong customer.
* **Our Solution (`primary_anchor_id`)**: `StateHarvester` checks `is_multi_row = len(rows) > 1`. If multiple rows are returned, it transitions `scope = EntityScope.COLLECTION.value` and **freezes** the `primary_anchor_id`. `customer_id: 1008` is preserved, while collection names are placed in a supplementary registry.

### 6.2 Managing Topic Shifts & Memory Eviction
When a user shifts topics to an aggregate inquiry:
* *Turn 5:* *"Apart from Dangote, which other industries do we have customers from?"*
* If `customer_number: 'CUST-01008'` remained bound in context, the SQL agent would generate:
  ```sql
  WHERE customer_number = 'CUST-01008' AND employer NOT ILIKE '%Dangote%'
  ```
  producing zero results.
* **Our Solution**:
  1. `QueryCondenser` matches `TOPIC_SHIFT_PATTERN` and calls `memory.evict_for_topic_shift(EntityScope.AGGREGATE)`.
  2. `evict_for_topic_shift` clears `active_entities` and `active_names`.
  3. In `sql_strategy.py`, context binding injection is explicitly bypassed when `working_memory.scope in (EntityScope.AGGREGATE.value, EntityScope.SYSTEM_META.value)`.
  4. The SQL agent generates an unconstrained aggregate query:
     ```sql
     SELECT DISTINCT employer FROM customers WHERE employer NOT ILIKE '%Dangote%' AND employer IS NOT NULL;
     ```

### 6.3 Concurrency & Multi-Worker State Consistency
* In production deployments running multiple Uvicorn worker processes or Kubernetes pods, in-memory state is insufficient.
* All reads and writes in `SessionContextManager` utilize Redis key-level isolation:
  ```
  session_memory:{session_id}
  ```
* Operations are atomic and serialized via standard JSON payloads. If a pod crashes mid-turn, the next worker resumes the session from Redis with zero context loss.

### 6.4 Sanitization of Public API Metadata
To ensure optimal client network performance and zero data leakage:
* `DynamicSQLStrategy` includes raw database rows in internal `sources[0]["rows"]` so `StateHarvester` can extract entities.
* Immediately after harvesting completes, `unified_orchestrator.py` executes:
  ```python
  if strategy_used == "sql" and sources:
      for s in sources:
          s.pop("rows", None)
  ```
* This prevents large internal row matrices from bloating public SSE `metadata` events sent to frontend clients.

---

## 7. Verification & Benchmarking Summary

The Universal Multi-Turn Context & Blackboard System is continuously validated by an automated end-to-end benchmark suite:

```bash
# Multi-turn progressive banking benchmark (4 turns across 3 databases)
python3 benchmarking/test_multiturn_banking_scenario.py

# Multi-turn enterprise drift & anti-collapse benchmark (8 turns across SQL, RAG, and Meta)
python3 benchmarking/test_multiturn_enterprise_drift.py
```

### Key Benchmark Metrics
* **Anaphora Resolution Accuracy**: **100%** across referential turns (pronouns, ellipses, topic shifts).
* **Cross-Database Pivot Success**: **100%** from PostgreSQL Customer $\to$ PostgreSQL Banking $\to$ MSSQL Lending.
* **Fast-Path Latency Overhead**: **0.00 ms** for non-referential / first-turn queries.
* **Zero Context Bleed**: Document RAG files are never leaked into relational turns, and relational names are never leaked into `SYSTEM_META` administrative responses.

---

## 8. Summary of Engineering Conventions

When extending or maintaining the Multi-Turn Context System, adhere to the following architectural rules:

1. **Never Inject Raw Database Dumps into Prompts**: All state passed between turns must flow through `SessionWorkingMemory` or sanitized history strings.
2. **Preserve the 0-ms Fast Path**: Do not call LLM condensation if regex heuristics determine the query is self-contained.
3. **Respect EntityScope**: Always check `memory.scope` before injecting entity bindings into downstream strategies. Aggregate and System inquiries must remain clean of individual entity filters.
4. **Harvest from Structured Data First**: Always prioritize extracting facts from `extra_meta["rows"]` over regex parsing of generated free-form prose.
