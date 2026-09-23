# Ingestion Layer Reference

> **Multi-Tenant RAG System** — Complete ingestion layer technical reference.
> Covers every stage from raw source bytes to searchable Qdrant/PGVector points.

---

## Table of Contents

1. [System Overview](#1-system-overview)
2. [Module Map](#2-module-map)
3. [The Ingestion Pipeline](#3-the-ingestion-pipeline)
4. [Stage 1 — Source Connectors](#4-stage-1--source-connectors)
5. [Stage 2 — Extraction & Parsing](#5-stage-2--extraction--parsing)
6. [Stage 3 — Chunking](#6-stage-3--chunking)
7. [Stage 4 — Embedding Generation](#7-stage-4--embedding-generation)
8. [Stage 5 — Dual-Write (Qdrant + PGVector)](#8-stage-5--dual-write-qdrant--pgvector)
9. [Async Worker Layer](#9-async-worker-layer)
10. [Storage Responsibilities](#10-storage-responsibilities)
11. [Lifecycle Management](#11-lifecycle-management)
12. [Security Layer](#12-security-layer)
13. [Key Classes & Functions Index](#13-key-classes--functions-index)

---

## 1. System Overview

The ingestion layer is the data-onboarding subsystem responsible for transforming raw content from any
source into semantically searchable vector embeddings. It is built around three design axes:

| Axis | Design Choice |
|------|---------------|
| **Connector abstraction** | `BaseConnector` / `RawDocument` — uniform interface regardless of source type |
| **Async execution** | Celery task queue backed by Redis; fan-out parallel worker pool |
| **Dual persistence** | Qdrant (primary vector search) + PostgreSQL/pgvector (relational backup & ACL) |

Every ingestion path — regardless of origin (HTTP upload, cloud storage sync, or live DB row) —
converges inside `UnifiedRAGOrchestrator.ingest_document()`.

---

## 2. Module Map

```
modules/
├── connectors/
│   ├── base.py                         # RawDocument dataclass; BaseConnector ABC
│   ├── registry.py                     # ConnectorRegistry — factory & descriptor catalogue
│   ├── schemas.py                      # Pydantic connector schemas for API layer
│   ├── parsers/
│   │   ├── base.py                     # BaseParser ABC
│   │   ├── factory.py                  # ParserFactory — extension→parser dispatch
│   │   ├── pdf_parser.py               # pypdf-based PDF extraction
│   │   ├── docx_parser.py              # python-docx Word extraction
│   │   ├── pptx_parser.py              # Zero-dep PPTX slide-by-slide extraction
│   │   ├── tabular_parser.py           # openpyxl / csv — Excel, CSV, TSV, pipe-delimited
│   │   └── text_parser.py              # Plain text / Markdown / JSON passthrough
│   ├── sources/
│   │   ├── file_connector.py           # In-memory file upload connector
│   │   ├── s3_connector.py             # AWS S3 / MinIO / Wasabi
│   │   ├── google_drive_connector.py   # Google Workspace Drive REST API
│   │   ├── sharepoint_connector.py     # Microsoft Graph API (SharePoint & OneDrive)
│   │   ├── notion_connector.py         # Notion REST API
│   │   ├── confluence_connector.py     # Atlassian Confluence Cloud/Server
│   │   └── databases/
│   │       ├── base_db_connector.py    # BaseDatabaseConnector (schema + row modes)
│   │       ├── postgres_connector.py   # PostgreSQLConnector
│   │       ├── mysql_connector.py      # MySQLConnector
│   │       ├── oracle_connector.py     # OracleDBConnector
│   │       ├── mssql_connector.py      # MSSQLConnector (MSSQL / Azure SQL)
│   │       └── schema_reflector.py     # DatabaseSchemaReflector — DDL introspection
│   └── security/
│       └── sql_guard.py                # SQLSecurityGuard — AST validation, SSRF, read-only sandbox
│
├── rag_core/
│   ├── orchestrator/
│   │   └── unified_orchestrator.py     # UnifiedRAGOrchestrator — ingest_document(), delete, update
│   ├── retrieval/
│   │   └── vector_store.py             # VectorStoreService — Qdrant HNSW wrapper
│   └── providers/
│       └── llm.py                      # LLMFactory, GeminiService, OllamaService, OpenAIService
│
├── governance/
│   ├── domain/
│   │   └── models.py                   # EnterpriseDocument, DocumentChunk, IngestionJob, DocumentStatus
│   └── repositories/
│       └── document_repository.py      # DocumentRepository — all DB CRUD for docs + jobs
│
└── tasks/
    ├── celery_app.py                   # Celery app wired to Redis broker/backend
    └── workers/
        └── ingestion_tasks.py          # async_ingest_document_task
                                        # async_ingest_connector_document_task
                                        # async_execute_ingestion_job_task

core/
├── storage.py                          # StorageManager — Claim-Check staging to disk
├── crypto.py                           # Fernet AES encryption for stored credentials
└── config.py                           # Settings (QDRANT_*, EMBEDDING_*, DB_*, REDIS_*, etc.)

app/routes/
├── documents.py                        # POST /documents/upload — HTTP entry point
└── jobs.py                             # POST /enterprise/jobs/create & /run
```

---

## 3. The Ingestion Pipeline

All content flows through these five sequential stages regardless of source:

```
SOURCE
  │
  ▼
[1] CONNECTOR  (BaseConnector.fetch_documents)
     Yields RawDocument(doc_id, source_type, filename, content_bytes, mime_type, metadata)
  │
  ▼
[2] PARSER  (ParserFactory.get_parser → BaseParser.parse)
     Converts binary content_bytes → clean Unicode text string
  │
  ▼
[3] CHUNKER  (UnifiedRAGOrchestrator.chunk_text)
     Splits text into semantically coherent chunks (≤4000 chars)
  │
  ▼
[4] EMBEDDER  (BaseLLMService.get_embeddings / get_embeddings_batch)
     Maps each text chunk → dense float32 vector (1024-dim default)
  │
  ▼
[5] DUAL-WRITE
     ├── Qdrant:       upsert_chunks(PointStruct[])  ← primary vector search
     └── PostgreSQL:   bulk_save_objects(DocumentChunk[]) via pgvector  ← source of truth
```

---

## 4. Stage 1 — Source Connectors

### 4.1 Contract: `BaseConnector` and `RawDocument`

**File:** `modules/connectors/base.py`

```python
@dataclass
class RawDocument:
    doc_id: str           # UUID generated by the connector
    source_type: str      # "file", "S3_BUCKET", "GOOGLE_DRIVE", etc.
    filename: str
    content_bytes: bytes  # Raw binary payload — not yet parsed
    mime_type: Optional[str]
    metadata: Dict[str, Any]  # external_id, last_modified, etag, etc.

class BaseConnector(ABC):
    @abstractmethod
    def fetch_documents(self) -> Generator[RawDocument, None, None]: ...
    @abstractmethod
    def test_connection(self) -> Dict[str, Any]: ...
```

Every connector implements these two methods. `fetch_documents()` is a **generator** — enabling
memory-efficient streaming of large source collections. `test_connection()` performs a pre-flight
credential handshake and latency check.

### 4.2 `ConnectorRegistry`

**File:** `modules/connectors/registry.py`

The static factory used by the Celery job orchestrator:

```python
ConnectorRegistry.get_connector(source_type, connection_config) -> BaseConnector
```

| `source_type` | Class | Category |
|---|---|---|
| `POSTGRES_DB` / `POSTGRESQL` | `PostgreSQLConnector` | Databases |
| `MYSQL_DB` / `MYSQL` | `MySQLConnector` | Databases |
| `ORACLE_DB` / `ORACLE` | `OracleDBConnector` | Databases |
| `MSSQL_DB` / `MSSQL` | `MSSQLConnector` | Databases |
| `S3_BUCKET` / `S3` | `S3Connector` | Cloud Storage |
| `GOOGLE_DRIVE` / `GSUITE` | `GoogleDriveConnector` | Cloud Storage |
| `SHAREPOINT` / `ONEDRIVE` | `SharePointConnector` | Cloud Storage |
| `CONFLUENCE` | `ConfluenceConnector` | Productivity |
| `NOTION` | `NotionConnector` | Productivity |

`ConnectorRegistry.get_descriptor_list()` returns typed UI schemas (field types, labels, secret flags)
used by the frontend to dynamically render connector configuration forms.

### 4.3 File Connector

**File:** `modules/connectors/sources/file_connector.py`

Used exclusively for direct HTTP uploads. Wraps in-memory bytes as a single `RawDocument`:

```python
class FileConnector(BaseConnector):
    def fetch_documents(self):
        yield RawDocument(doc_id=uuid4(), source_type="file", ...)
```

Instantiated inside `UnifiedRAGOrchestrator.ingest_document()` — bypasses the job queue.

### 4.4 Cloud Storage Connectors

**S3Connector** — Uses `boto3` paginator to lazily enumerate all objects under a configured prefix.
For each non-directory key, calls `s3.get_object()` and yields `RawDocument` with
`external_id=s3_key`, `etag`, and `last_modified`.

**GoogleDriveConnector** — Authenticates via direct access token or Service Account JSON (JWT signed
with RS256, posted to the Google token endpoint). Lists files via `drive.googleapis.com/drive/v3/files`
and downloads each via the `?alt=media` endpoint.

**SharePointConnector** — Authenticates via Azure AD OAuth 2.0 client credentials flow. Discovers
all document libraries for the site via Microsoft Graph, then does breadth-first folder traversal
with `@odata.nextLink` pagination. Files are downloaded using `@microsoft.graph.downloadUrl`.

**NotionConnector** — Searches all pages via `POST /v1/search`. Fetches block children per page,
joining `rich_text.plain_text` values into a UTF-8 document.

**ConfluenceConnector** — Uses HTTP Basic Auth (email + API token). Queries
`/rest/api/space/{space_key}/content` and fetches page body, stripping HTML tags.

### 4.5 Database Connectors

**File:** `modules/connectors/sources/databases/base_db_connector.py`

All four database connectors extend `BaseDatabaseConnector`, which implements `fetch_documents()`
in two modes selected at job-creation time:

#### Mode A: `SCHEMA_REFLECTION`

1. `DatabaseSchemaReflector.reflect_schema()` — uses SQLAlchemy `inspect()` to enumerate tables,
   extract column names/types, PKs, FKs, and sample up to 3 distinct non-null values per text column.
2. `DatabaseSchemaReflector.generate_schema_documents()` compiles each table into a DDL document.
3. Each `RawDocument` has `mime_type="application/sql"`, `metadata.chunk_type="sql_schema"`.
   These documents enable the Text-to-SQL agent.

#### Mode B: `ROW_EXTRACTION`

1. Validates user SQL with `SQLSecurityGuard.validate_query()` (AST-level; blocks writes).
2. Executes via `SQLSecurityGuard.execute_read_only_query()` inside a read-only transaction sandbox.
3. Streams rows in batches (`EXTERNAL_DB_FETCH_BATCH_SIZE`, default 500).
4. Each row is serialised to text: uses `text_column` if specified; otherwise auto-detects a content
   field (`content`, `body`, `description`…); falls back to `Key: Value` pairs.
5. Yields one `RawDocument` per database row.

---

## 5. Stage 2 — Extraction & Parsing

### 5.1 `ParserFactory`

**File:** `modules/connectors/parsers/factory.py`

Stateless extension→parser dispatch table:

```python
_parsers = {
    ".pdf":  PDFParser(),
    ".docx": DocxParser(),  ".doc": DocxParser(),
    ".pptx": PPTXParser(),  ".ppt": PPTXParser(),
    ".txt":  TextParser(),  ".md": TextParser(),  ".json": TextParser(),
    ".xlsx": TabularParser(), ".xls": TabularParser(),
    ".csv":  TabularParser(), ".tsv": TabularParser(),
}
```

Falls back to `TabularParser` if `mime_type` contains `"spreadsheet"`, `"excel"`, or `"csv"`.
Falls back to `TextParser` for all other unknown extensions.

### 5.2 Parser Implementations

| Parser | Library | Output Format |
|---|---|---|
| `PDFParser` | `pypdf.PdfReader` | Page-by-page text with `\n` between pages |
| `DocxParser` | `python-docx` | Paragraphs + table rows joined by ` \| ` |
| `PPTXParser` | stdlib `zipfile` + `xml.etree` | `### [Slide N]\n<text tokens>` per slide |
| `TabularParser` | `openpyxl` (xlsx) / `csv.Sniffer` (csv/tsv) | `[Sheet: X \| Row N] Col: Val \| Col: Val` |
| `TextParser` | built-in `decode()` | Raw UTF-8 or Latin-1 passthrough |

**`PPTXParser`** is zero-dependency — unzips the OpenXML package using Python's stdlib, iterates
`ppt/slides/slideN.xml` in natural numeric order, collects all DrawingML `{...}t` text run elements.

**`TabularParser`** serialises each row as a self-contained semantic record preserving header names:
`[Sheet: Sales | Row 1] Product: Widget | Price: 9.99 | ...`. This prevents information loss when
chunks are retrieved in isolation from the rest of the document.

The `parse(content_bytes: bytes) -> str` interface is called inside `extract_text_from_file()`:

```python
parser = ParserFactory.get_parser(filename, mime_type)
raw_text = parser.parse(content_bytes)
```

---

## 6. Stage 3 — Chunking

**Method:** `UnifiedRAGOrchestrator.chunk_text(text, max_chars=4000)`

The chunker is paragraph-aware. It splits on double newlines (`\n\n`) first, then handles oversized
paragraphs by splitting on sentence boundaries (`. `, `! `, `? `). The algorithm:

1. Split text on `\n\n` to get logical paragraphs.
2. Accumulate paragraphs into a rolling buffer.
3. When the buffer would exceed `max_chars`, flush it as a completed chunk and start a new one.
4. For paragraphs larger than `max_chars`, split on sentence boundaries.

This semantic-aware approach keeps related sentences and bullet points together, improving retrieval
coherence for each embedded chunk.

---

## 7. Stage 4 — Embedding Generation

**Method:** `BaseLLMService.get_embeddings(text: str) -> List[float]`

`LLMFactory.get_provider()` reads `settings.LLM_PROVIDER` and returns the appropriate service:

| `LLM_PROVIDER` | Embedding Model | Generation Model |
|---|---|---|
| `gemini` | `text-embedding-004` (Google GenAI SDK) | `gemini-2.5-flash` |
| `ollama` | `qwen3-embedding:0.6b` (local) | `gemma3:12b` |
| `openai` | `text-embedding-3-small` | `gpt-4o-mini` |
| `vllm` | `VLLM_EMBEDDING_MODEL` (configurable) | configurable |

Batch embedding is used when available:

```python
if hasattr(self.llm, "get_embeddings_batch"):
    embeddings = self.llm.get_embeddings_batch(chunks)
else:
    embeddings = [self.llm.get_embeddings(c) for c in chunks]
```

Default vector dimension: **1024** (configurable via `EMBEDDING_DIMENSION` env var).

---

## 8. Stage 5 — Dual-Write (Qdrant + PGVector)

### 8.1 Qdrant — Primary Vector Search

**File:** `modules/rag_core/retrieval/vector_store.py`

Each chunk becomes a `PointStruct`:

```python
PointStruct(
    id=point_id,           # UUID — shared with PostgreSQL chunk row
    vector=embedding,      # List[float], 1024-dim cosine
    payload={
        "document_id": ...,
        "org_id": ...,     # Tenant isolation
        "department_id": ...,
        "uploader_id": ...,
        "access_level": ...,  # PUBLIC / DEPARTMENT / CONFIDENTIAL
        "session_id": ...,    # Empty for enterprise docs
        "scope": ...,         # "enterprise" | "session"
        "filename": ...,
        "chunk_index": ...,
        "content": ...,       # Full chunk text stored in payload
        "source_type": ...
    }
)
```

**Collection configuration:**
- Distance metric: **COSINE**
- `on_disk=False` — vectors kept in RAM for sub-30ms retrieval SLA
- HNSW: `m=16`, `ef_construct=100`, `ef_search=64`

**Payload indexes** (keyword type): `org_id`, `department_id`, `access_level`,
`uploader_id`, `session_id`, `document_id` — enabling O(1) multi-tenant filtering.

### 8.2 PostgreSQL / pgvector — Source of Truth

Each chunk is also saved as a `DocumentChunk` SQLAlchemy row with the **same UUID** as the
corresponding Qdrant point — maintaining a bijective mapping:

```python
DocumentChunk(
    id=point_id,           # Same UUID as Qdrant point
    document_id=...,
    org_id=...,
    department_id=...,
    access_level=...,
    chunk_index=...,
    content=...,
    embedding=...,         # pgvector Vector(1024) column
    metadata_json={source_type, filename, session_id, scope}
)
```

Before inserting new chunks, all existing chunks for the document are deleted:

```python
db.query(DocumentChunk).filter(DocumentChunk.document_id == document_id).delete()
db.bulk_save_objects(db_chunk_records)
```

### 8.3 Dual-Write Consistency

If the PostgreSQL write fails after a Qdrant write succeeds, the orchestrator performs an automatic
Qdrant rollback:

```python
except Exception as db_err:
    db.rollback()
    doc_filter = Filter(must=[FieldCondition(key="document_id", match=MatchValue(value=document_id))])
    self.vector_store.delete_by_filter(doc_filter)
    raise db_err
```

Both stores are either present or absent — no partial-write states.

---

## 9. Async Worker Layer

**Files:** `modules/tasks/celery_app.py`, `modules/tasks/workers/ingestion_tasks.py`

### 9.1 Celery App

```python
celery_app = Celery(
    "enterprise_rag_tasks",
    broker=REDIS_URL,
    backend=REDIS_URL,
    include=["modules.tasks.workers.ingestion_tasks"]
)
# Serializer: JSON | Task time limit: 3600s | Task tracking: enabled
```

### 9.2 Three Celery Tasks

#### `async_ingest_document_task` — Direct Upload Worker

Triggered by `POST /documents/upload`:

1. Read bytes from staged disk path (`StorageManager.read_staged_file`)
2. Call `orchestrator.ingest_document()`
3. Update `DocumentStatus` → `INDEXED` with `chunk_count`
4. Delete the staged file

#### `async_execute_ingestion_job_task` — Connector Job Orchestrator

Triggered by `POST /enterprise/jobs/{id}/run`:

1. **Decrypt** connection config via Fernet AES
2. **Instantiate** the connector via `ConnectorRegistry.get_connector`
3. **CDC loop** over `connector.fetch_documents()`:
   - Compute `SHA-256(content_bytes)` for each `RawDocument`
   - Look up existing `EnterpriseDocument` by `external_id`
   - **Skip** if hash unchanged and status is `INDEXED`
   - **Update** record if hash changed; **create** if new
   - Stage bytes to disk
   - Append a `async_ingest_connector_document_task.s(...)` to the subtask list
4. **Purge** documents no longer present in the source
5. **Fan-out** all subtasks in parallel via `celery.group(subtasks).apply_async()`

#### `async_ingest_connector_document_task` — Connector Document Sub-Worker

One per file, executed concurrently:

1. Read staged bytes
2. Call `orchestrator.ingest_document()`
3. Mark `INDEXED` + chunk count
4. Delete staged file

### 9.3 The Claim-Check Pattern

Large files are never serialised through Redis. Instead:

```
API handler  →  StorageManager.save_staged_file()
                writes to /app/storage/staging/{org_id}/{doc_id}_{filename}
             →  passes staged_path (string) to Celery via Redis message

Worker       →  StorageManager.read_staged_file(staged_path)
             →  StorageManager.delete_staged_file(staged_path)  # on success or failure
```

---

## 10. Storage Responsibilities

### Qdrant

| Responsibility | Implementation |
|---|---|
| Store embeddings | `PointStruct.vector` — cosine, HNSW, in-RAM |
| Store chunk text | `PointStruct.payload.content` |
| Multi-tenant isolation | Payload filter on `org_id` at every query |
| Access control | Payload filter on `access_level`, `department_id` |
| Vector deletion | `delete_by_filter(Filter(...))` |
| Metadata updates | `set_payload_by_filter(Filter(...), payload)` |

### PostgreSQL / pgvector (`document_chunks`)

| Responsibility | Implementation |
|---|---|
| Chunk text storage | `content TEXT` column |
| Vector backup | `embedding Vector(1024)` pgvector column |
| Relational FK integrity | `document_id → enterprise_documents`, `org_id → organizations` |
| Cascading deletes | `ondelete="CASCADE"` on document_id FK |
| ACL updates | SQL `UPDATE` on `access_level`, `department_id` |
| Composite indexes | `(org_id, department_id)`, `(document_id, chunk_index)` |

### PostgreSQL (`enterprise_documents`)

Tracks document lifecycle:

```
id, org_id, department_id, uploader_id, filename,
file_hash (SHA-256), file_size_bytes, mime_type,
access_level (PUBLIC/DEPARTMENT/CONFIDENTIAL),
status (PENDING→PROCESSING→INDEXED/FAILED),
chunk_count, error_message,
job_id (FK → ingestion_jobs),
external_id (source-native ID for CDC),
external_last_modified
```

### PostgreSQL (`ingestion_jobs`)

Configuration and state for connector sync jobs:

```
id, org_id, department_id, created_by_id, name,
source_type, access_level,
connection_config (Fernet-encrypted JSON),
cron_schedule, status (PENDING/RUNNING/COMPLETED/FAILED),
last_run_at, documents_processed_count, error_message
```

### Staging Storage (`/app/storage/staging`)

Managed by `core/storage.py — StorageManager`. Transient on-disk storage for the Claim-Check pattern.
Files are organised as `{staging_dir}/{org_id}/{doc_id}_{safe_filename}` and deleted after ingestion.

---

## 11. Lifecycle Management

### 11.1 Initial Indexing

**File Upload:**
1. `POST /documents/upload` → validate → create `EnterpriseDocument` (status=`PENDING`)
2. Stage bytes to disk
3. Dispatch `async_ingest_document_task.delay(...)`
4. Worker: parse → chunk → embed → dual-write → mark `INDEXED`

**Connector Job (first run):**
1. `POST /enterprise/jobs/{id}/run` → dispatch `async_execute_ingestion_job_task.delay(job_id)`
2. Orchestrator iterates all source documents; no existing records → creates new `EnterpriseDocument` rows
3. Fan-out subtasks → all documents processed in parallel across the worker pool

### 11.2 Incremental Updates (CDC)

For connector jobs on subsequent runs:

```python
file_hash = hashlib.sha256(raw_doc.content_bytes).hexdigest()
if existing_doc and existing_doc.file_hash == file_hash and existing_doc.status == INDEXED:
    skipped_count += 1
    continue  # No change — skip entirely
```

If hash differs: update the record, re-stage, and queue `async_ingest_connector_document_task`.
The orchestrator deletes old chunks and writes new ones, making this an idempotent re-index.

### 11.3 Re-indexing

`ingest_document()` always deletes all existing `DocumentChunk` rows before inserting new ones.
Qdrant vectors are replaced via upsert (same `point_id` UUID overwrites existing vector payload).
To force a re-index, reset the document status to `PENDING` and dispatch a new task.

### 11.4 Deletions

**Manual delete (user):**
- `orchestrator.delete_document_vectors(document_id, org_id, db)` — purges Qdrant points and
  `DocumentChunk` rows; cascading FK removes chunks when `EnterpriseDocument` is deleted.

**Source deletion (connector sync):**
- `purge_deleted_external_documents()` compares `active_external_ids` collected during the sync
  cycle against all stored `external_id` values for that job.
- Documents no longer present in the source are purged from Qdrant and deleted from PostgreSQL.

### 11.5 Metadata Updates (ACL Propagation)

`orchestrator.update_document_metadata(document_id, org_id, access_level, department_id, db)`:

1. Updates Qdrant payload on all matching points via `set_payload_by_filter`
2. Updates `access_level`/`department_id` on all `DocumentChunk` rows via SQL UPDATE
3. Updates the `EnterpriseDocument` record

This ensures ACL changes propagate to the vector store immediately without re-embedding.

### 11.6 Source Synchronization Schedule

`IngestionJob.cron_schedule` stores a cron expression. An external scheduler (Celery Beat or
external trigger) invokes `POST /enterprise/jobs/{id}/run` on schedule. Each run is a full
CDC cycle — skip unchanged, update changed, purge deleted.

---

## 12. Security Layer

### Credential Encryption

**File:** `core/crypto.py`

All `connection_config` dicts are Fernet-encrypted before storage in the `ingestion_jobs` table.
The key is derived from `DB_ENCRYPTION_KEY` (or `ENTERPRISE_SECRET_KEY`) via SHA-256 → URL-safe Base64.
At job execution time, `decrypt_connection_config()` decrypts before passing to the connector.

### SQL Security Guard

**File:** `modules/connectors/security/sql_guard.py`

| Protection | Mechanism |
|---|---|
| SSRF prevention | Validates DB host against `ALLOWED_DB_PRIVATE_HOSTS` and `ALLOWED_DB_PRIVATE_CIDRS` |
| Read-only enforcement | `SET SESSION CHARACTERISTICS AS TRANSACTION READ ONLY` (PG); `SET SESSION TRANSACTION READ ONLY` (MySQL); `SET TRANSACTION READ ONLY` (Oracle); `ApplicationIntent=ReadOnly` (MSSQL) |
| AST query validation | Parses SQL via `sqlglot`; rejects any non-SELECT statement type |
| Statement timeout | `EXTERNAL_DB_STATEMENT_TIMEOUT_MS` (default 15000ms) |
| Row batch streaming | `EXTERNAL_DB_FETCH_BATCH_SIZE` (default 500 rows/batch) |
| URL masking | Passwords and sensitive query keys masked in all log output |

### Multi-Tenant Isolation

Every Qdrant query includes `org_id=current_user.org_id` in the payload filter. `RAGSecurityFilterBuilder`
constructs the appropriate `Filter` based on the user's role and department at retrieval time.

---

## 13. Key Classes & Functions Index

| Class / Function | File | Responsibility |
|---|---|---|
| `BaseConnector` | `modules/connectors/base.py` | Connector contract |
| `RawDocument` | `modules/connectors/base.py` | Canonical source document DTO |
| `ConnectorRegistry` | `modules/connectors/registry.py` | Connector factory + UI descriptor catalogue |
| `ParserFactory` | `modules/connectors/parsers/factory.py` | Extension→Parser dispatch |
| `PDFParser` | `modules/connectors/parsers/pdf_parser.py` | pypdf text extraction |
| `TabularParser` | `modules/connectors/parsers/tabular_parser.py` | Excel/CSV/TSV semantic row serialiser |
| `PPTXParser` | `modules/connectors/parsers/pptx_parser.py` | Zero-dep PPTX slide extraction |
| `S3Connector` | `modules/connectors/sources/s3_connector.py` | AWS S3 / MinIO document streamer |
| `GoogleDriveConnector` | `modules/connectors/sources/google_drive_connector.py` | Google Workspace Drive ingestion |
| `SharePointConnector` | `modules/connectors/sources/sharepoint_connector.py` | Microsoft Graph recursive library traversal |
| `BaseDatabaseConnector` | `modules/connectors/sources/databases/base_db_connector.py` | Schema reflection + row extraction modes |
| `DatabaseSchemaReflector` | `modules/connectors/sources/databases/schema_reflector.py` | SQLAlchemy DDL introspection + sample extraction |
| `SQLSecurityGuard` | `modules/connectors/security/sql_guard.py` | AST validation, SSRF, read-only sandbox |
| `UnifiedRAGOrchestrator` | `modules/rag_core/orchestrator/unified_orchestrator.py` | Central ingestion + retrieval orchestrator |
| `UnifiedRAGOrchestrator.ingest_document()` | same | Parse → chunk → embed → dual-write |
| `UnifiedRAGOrchestrator.chunk_text()` | same | Paragraph-aware text splitter |
| `UnifiedRAGOrchestrator.delete_document_vectors()` | same | Qdrant + PG deletion |
| `UnifiedRAGOrchestrator.update_document_metadata()` | same | ACL propagation to Qdrant payloads + PG chunks |
| `VectorStoreService` | `modules/rag_core/retrieval/vector_store.py` | Qdrant HNSW client wrapper |
| `VectorStoreService.upsert_chunks()` | same | Batch vector upsert |
| `VectorStoreService.delete_by_filter()` | same | Filtered vector deletion |
| `VectorStoreService.set_payload_by_filter()` | same | Live payload metadata updates |
| `LLMFactory` | `modules/rag_core/providers/llm.py` | Provider selection (Gemini/Ollama/OpenAI/vLLM) |
| `DocumentRepository` | `modules/governance/repositories/document_repository.py` | All document + job CRUD |
| `DocumentRepository.purge_deleted_external_documents()` | same | CDC deletion tracking |
| `EnterpriseDocument` | `modules/governance/domain/models.py` | Document lifecycle ORM model |
| `DocumentChunk` | `modules/governance/domain/models.py` | Chunk + pgvector embedding ORM model |
| `IngestionJob` | `modules/governance/domain/models.py` | Connector sync job ORM model |
| `StorageManager` | `core/storage.py` | Claim-Check disk staging |
| `encrypt_connection_config()` | `core/crypto.py` | Fernet credential encryption |
| `decrypt_connection_config()` | `core/crypto.py` | Fernet credential decryption |
| `async_ingest_document_task` | `modules/tasks/workers/ingestion_tasks.py` | File upload Celery task |
| `async_execute_ingestion_job_task` | `modules/tasks/workers/ingestion_tasks.py` | Connector CDC orchestrator |
| `async_ingest_connector_document_task` | `modules/tasks/workers/ingestion_tasks.py` | Per-file connector sub-task |
