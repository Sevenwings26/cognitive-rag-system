# How This Platform Turns Files, Cloud Storage, and Live Databases into Searchable Knowledge

*A technical deep-dive into the ingestion layer of a multi-tenant enterprise RAG system.*

---

Building a production-grade Retrieval-Augmented Generation (RAG) system is far more than wrapping
a language model around a vector database. The hardest problem isn't retrieval — it's ingestion.
Turning heterogeneous, unstructured enterprise content into semantically searchable knowledge chunks
requires a thoughtful, multi-stage pipeline that handles dozens of file formats, live external data
sources, change detection, access control, and fault tolerance at scale.

This article traces the exact code paths this platform uses to go from a raw file — or a SharePoint
library, or a PostgreSQL table — to a searchable Qdrant vector point.

---

## The Fundamental Abstraction: `RawDocument`

Every piece of content in the system, regardless of origin, is normalised into a single intermediate
object defined in `modules/connectors/base.py`:

```python
@dataclass
class RawDocument:
    doc_id: str
    source_type: str      # "S3_BUCKET", "GOOGLE_DRIVE", "POSTGRES_DB", etc.
    filename: str
    content_bytes: bytes  # Raw binary — PDF bytes, DOCX bytes, UTF-8 text, etc.
    mime_type: Optional[str]
    metadata: Dict[str, Any]  # external_id, last_modified, etag, etc.
```

The abstract `BaseConnector` contract requires all connectors to implement a `fetch_documents()`
generator that yields `RawDocument` objects. This single interface means the downstream pipeline
— parsing, chunking, embedding, indexing — is completely source-agnostic.

---

## Stage 1: Getting the Content

### Direct File Uploads

When a user uploads a file via `POST /documents/upload`, the API handler in
`app/routes/documents.py` immediately does two things:

1. Creates an `EnterpriseDocument` row in PostgreSQL with status `PENDING` and the SHA-256 hash
   of the file.
2. Writes the binary payload to the staging disk at
   `/app/storage/staging/{org_id}/{doc_id}_{filename}` using `StorageManager.save_staged_file()`.

It then enqueues a `async_ingest_document_task` Celery task with just the **staged file path** —
not the file bytes themselves. This is the **Claim-Check Pattern**: instead of serialising megabytes
of binary data through Redis, the task message contains a lightweight file path string. The Celery
worker retrieves the bytes from disk.

### Cloud Storage Sources

For S3, Google Drive, SharePoint, Notion, and Confluence, users configure an **Ingestion Job** —
a persisted record in the `ingestion_jobs` table that stores encrypted connection credentials
and an optional cron schedule.

When the job runs, `async_execute_ingestion_job_task` (in `modules/tasks/workers/ingestion_tasks.py`)
decrypts the credentials using Fernet AES, instantiates the appropriate connector via
`ConnectorRegistry.get_connector()`, and iterates `connector.fetch_documents()`.

The **S3Connector** uses a `boto3` paginator to lazily stream object keys from a bucket prefix —
no full listing required for large buckets. The **SharePointConnector** discovers all document
libraries for a site via Microsoft Graph and does a BFS traversal of the folder tree, including
pagination via `@odata.nextLink`. **GoogleDriveConnector** authenticates with a Service Account
JSON key, creating a short-lived OAuth2 JWT to get a bearer token, then lists files via the Drive
REST API.

### Live Databases

Database connectors operate in two modes:

**Schema Reflection mode** introspects the live schema using `DatabaseSchemaReflector`, which uses
SQLAlchemy's `inspect()` to extract table DDL, column types, primary keys, foreign keys, and even
samples 3 distinct non-null values from each text column. It compiles these into structured DDL
documents that become the knowledge base for the platform's Text-to-SQL agent.

**Row Extraction mode** executes a user-provided SQL query validated by `SQLSecurityGuard` — an
AST-level SQL parser that rejects anything that isn't a `SELECT` statement, enforces read-only
transactions at the engine level, applies a 15-second statement timeout, and streams rows in
batches of 500 to prevent memory exhaustion.

---

## Stage 2: Parsing — Format-Specific Text Extraction

`ParserFactory` in `modules/connectors/parsers/factory.py` maps file extensions to parser instances.
All parsers implement a single method: `parse(content_bytes: bytes) -> str`.

**PDF files** are handled by `PDFParser` using `pypdf`, extracting text page by page.

**Word documents** use `DocxParser` via `python-docx`, extracting paragraphs and serialising
table rows as pipe-separated text (`cell1 | cell2 | cell3`).

**PowerPoint files** are extracted by `PPTXParser` using zero external dependencies — it unzips
the `.pptx` OpenXML archive using Python's stdlib, parses `ppt/slides/slideN.xml` in natural
numeric order, and collects all DrawingML text run elements (`{...}t` tags).

**Spreadsheets** are the most nuanced. `TabularParser` uses `openpyxl` for `.xlsx` files and
Python's `csv.Sniffer` for comma/tab/semicolon/pipe-delimited files. Crucially, each row is
serialised as a self-contained semantic record that preserves column headers:

```
[Sheet: Revenue | Row 1] Product: Enterprise License | Q1: 142000 | Q2: 156000
```

This format ensures that when a chunk is retrieved in isolation, it still carries full context
about what each value means — critical for correct LLM interpretation.

---

## Stage 3: Chunking — Paragraph-Aware Splitting

`UnifiedRAGOrchestrator.chunk_text()` splits extracted text into chunks of at most 4000 characters.
Rather than naively splitting on character counts, it respects the document's semantic structure:

1. Split on `\n\n` (paragraph boundaries) first.
2. Accumulate paragraphs in a rolling buffer until adding the next would exceed `max_chars`.
3. Flush the buffer as a complete chunk and start fresh.
4. For paragraphs that individually exceed `max_chars`, split on sentence boundaries.

This means a bullet-point list stays together as a chunk, and a single long sentence isn't
arbitrarily cut mid-thought.

---

## Stage 4: Embedding — Multi-Provider Vector Generation

The platform supports four embedding backends, selected via the `LLM_PROVIDER` environment variable:
Google Gemini (`text-embedding-004`), Ollama (`qwen3-embedding:0.6b` locally), OpenAI
(`text-embedding-3-small`), or vLLM with a configurable model.

All chunks for a document are embedded in a single batch call where supported:

```python
embeddings = self.llm.get_embeddings_batch(chunks)  # preferred
# fallback: [self.llm.get_embeddings(c) for c in chunks]
```

The default vector dimensionality is **1024**, tunable via `EMBEDDING_DIMENSION`.

---

## Stage 5: Dual-Write — Qdrant as Primary, PostgreSQL as Truth

This is where the architecture makes a deliberate, principled choice: **every chunk is written to
two stores simultaneously**.

**Qdrant** receives a `PointStruct` for each chunk containing the embedding vector and a rich
payload including `org_id`, `department_id`, `access_level`, `session_id`, `document_id`,
`chunk_index`, and the full chunk text. The collection uses cosine distance, HNSW with `m=16`
and `ef_construct=100`, and all vectors are kept in RAM (`on_disk=False`) for a sub-30ms retrieval
SLA. Payload indexes on `org_id`, `department_id`, and `access_level` enable instant multi-tenant
filtered search.

**PostgreSQL** receives a `DocumentChunk` row for each chunk with **the same UUID** as the Qdrant
point — maintaining a bijective mapping. The `embedding` column uses pgvector's `Vector(1024)` type.
This relational copy serves as the disaster-recovery backup: if Qdrant data is lost, it can be
rebuilt from PostgreSQL.

If the PostgreSQL write fails after the Qdrant write succeeds, the orchestrator immediately
issues a `delete_by_filter` call to Qdrant to roll back those vectors — ensuring the two stores
are never in a split-brain state.

---

## Change Data Capture: Detecting What Changed

The connector job orchestrator runs a CDC loop on every execution:

```python
file_hash = hashlib.sha256(raw_doc.content_bytes).hexdigest()
existing_doc = DocumentRepository.get_document_by_external_id(...)

if existing_doc and existing_doc.file_hash == file_hash and existing_doc.status == INDEXED:
    skipped_count += 1
    continue  # Unchanged — no embedding cost, no write I/O
```

Only modified files (hash mismatch) or new files (no existing record) are processed. At the end
of each sync cycle, the orchestrator collects the set of `external_id` values seen in the source
and calls `purge_deleted_external_documents()` — which identifies documents whose `external_id` is
no longer present and deletes their vectors from both Qdrant and PostgreSQL.

---

## The Fan-Out Pattern: Parallelism at Scale

For connector jobs with hundreds or thousands of files, processing them sequentially would be
prohibitively slow. The orchestrator builds a Celery `group` of `async_ingest_connector_document_task`
signatures and dispatches them all at once:

```python
job_group = group(subtasks)
job_group.apply_async()
```

Each subtask runs independently on a Celery worker, enabling the full worker pool to process
documents in parallel. The job orchestrator itself returns immediately after dispatching the group —
it doesn't wait for individual subtasks to complete.

---

## Security: Protecting the Ingestion Surface

The ingestion layer has three security concerns, each addressed explicitly:

**Credential protection:** All `connection_config` dicts (containing DB passwords, API tokens,
service account keys) are Fernet-encrypted with AES-128 before storage in the `ingestion_jobs`
table. The encryption key is derived from an application secret via SHA-256.

**SQL injection prevention:** Database connectors run user-supplied SQL through `SQLSecurityGuard`,
which uses `sqlglot` to parse the query AST and reject anything other than `SELECT`. Execution
happens inside an explicit read-only transaction with a configurable statement timeout.

**SSRF protection:** Database host/IP addresses are validated against an allowlist
(`ALLOWED_DB_PRIVATE_HOSTS`) and CIDR ranges before any connection is attempted.

---

## Putting It Together

The complete journey of a file — from user upload to searchable knowledge — involves:

1. An HTTP upload handler that stages bytes to disk and enqueues a Celery task
2. A Celery worker that reads staged bytes and calls into the orchestrator
3. A `FileConnector` wrapping the bytes as a `RawDocument`
4. A `ParserFactory` selecting the right parser by file extension
5. A paragraph-aware chunker splitting text into coherent segments
6. A batch embedding call to the configured LLM provider
7. A dual atomic write to Qdrant (vectors + payload) and PostgreSQL (chunks + pgvector)
8. A document status update marking the record `INDEXED`
9. Staged file cleanup

For cloud connectors, the same pipeline runs across a parallel Celery worker pool, with an outer
CDC loop detecting new, changed, and deleted files on every scheduled sync.

The result: any document, database row, or wiki page — regardless of format or source — becomes
a semantically searchable, access-controlled knowledge chunk with full lifecycle management,
multi-tenant isolation, and disaster-recovery persistence.
