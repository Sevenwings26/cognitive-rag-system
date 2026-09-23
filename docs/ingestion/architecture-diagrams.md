# Ingestion Architecture Diagrams

> Mermaid diagrams for the multi-tenant RAG system ingestion layer.

---

## 1. High-Level Ingestion Architecture

```mermaid
graph TD
    subgraph API["FastAPI Application"]
        U[POST /documents/upload]
        J[POST /enterprise/jobs/:id/run]
    end

    subgraph Queue["Async Layer (Celery + Redis)"]
        T1[async_ingest_document_task]
        T2[async_execute_ingestion_job_task]
        T3[async_ingest_connector_document_task\n×N parallel workers]
    end

    subgraph Connectors["Source Connectors"]
        FC[FileConnector]
        S3[S3Connector]
        GD[GoogleDriveConnector]
        SP[SharePointConnector]
        NO[NotionConnector]
        CO[ConfluenceConnector]
        DB[BaseDatabaseConnector\nPostgreSQL · MySQL · Oracle · MSSQL]
    end

    subgraph Orchestrator["UnifiedRAGOrchestrator"]
        EX[extract_text_from_file\nParserFactory]
        CH[chunk_text]
        EM[get_embeddings_batch\nLLMService]
        DW[Dual-Write]
    end

    subgraph Storage["Persistent Storage"]
        QD[(Qdrant\nHNSW Vector Index)]
        PG[(PostgreSQL\nDocumentChunk + pgvector)]
        PGMETA[(PostgreSQL\nEnterpriseDocument\nIngestionJob)]
        DISK[/app/storage/staging\nClaim-Check Disk]
    end

    U --> T1
    J --> T2
    T2 --> T3

    T1 --> FC --> Orchestrator
    T2 --> S3 & GD & SP & NO & CO & DB
    S3 & GD & SP & NO & CO & DB --> T3
    T3 --> Orchestrator

    Orchestrator --> EX --> CH --> EM --> DW
    DW --> QD
    DW --> PG
    T1 & T2 & T3 --> DISK
    T2 --> PGMETA
```

---

## 2. File Upload Flow

```mermaid
sequenceDiagram
    participant Client
    participant API as FastAPI\n/documents/upload
    participant DB as PostgreSQL\nenterprise_documents
    participant Disk as Staging Disk\n/storage/staging
    participant Redis as Redis\nCelery Broker
    participant Worker as Celery Worker
    participant Orch as UnifiedRAGOrchestrator
    participant QD as Qdrant
    participant PG as PostgreSQL\ndocument_chunks

    Client->>API: POST /documents/upload\n(file, access_level, session_id)
    API->>API: sha256(file_bytes) → file_hash
    API->>DB: CREATE EnterpriseDocument\nstatus=PENDING
    API->>Disk: save_staged_file()\n→ {doc_id}_{filename}
    API->>Redis: async_ingest_document_task.delay(\n  document_id, filename,\n  staged_path, org_id, ...)
    API-->>Client: 202 Accepted\n{document_id, task_id}

    Redis->>Worker: dequeue task
    Worker->>DB: UPDATE status=PROCESSING
    Worker->>Disk: read_staged_file(staged_path)
    Worker->>Orch: ingest_document(filename, file_bytes, ...)

    Orch->>Orch: FileConnector → RawDocument
    Orch->>Orch: ParserFactory.get_parser(filename)
    Orch->>Orch: parser.parse(content_bytes) → text
    Orch->>Orch: chunk_text(text) → chunks[]
    Orch->>Orch: llm.get_embeddings_batch(chunks) → embeddings[]

    loop for each (chunk, embedding)
        Orch->>Orch: build PointStruct(id, vector, payload)
        Orch->>Orch: build DocumentChunk(id, content, embedding)
    end

    Orch->>QD: upsert_chunks(points[])
    Orch->>PG: DELETE old chunks WHERE document_id=...
    Orch->>PG: bulk_save_objects(db_chunk_records[])

    Worker->>DB: UPDATE status=INDEXED, chunk_count=N
    Worker->>Disk: delete_staged_file(staged_path)
```

---

## 3. Cloud Storage Ingestion Flow (Connector Job)

```mermaid
sequenceDiagram
    participant Trigger as Scheduler / Admin
    participant API as FastAPI\n/enterprise/jobs/:id/run
    participant PGJ as PostgreSQL\ningestion_jobs
    participant Redis as Redis\nCelery Broker
    participant Orch as Job Orchestrator\nasync_execute_ingestion_job_task
    participant Conn as Cloud Connector\n(S3/Drive/SharePoint/Notion/Confluence)
    participant PGD as PostgreSQL\nenterprise_documents
    participant Disk as Staging Disk
    participant Workers as Worker Pool\nasync_ingest_connector_document_task ×N
    participant QD as Qdrant
    participant PGC as PostgreSQL\ndocument_chunks

    Trigger->>API: POST /enterprise/jobs/{id}/run
    API->>Redis: async_execute_ingestion_job_task.delay(job_id)
    API-->>Trigger: 202 Accepted

    Redis->>Orch: dequeue
    Orch->>PGJ: SET status=RUNNING
    Orch->>Orch: decrypt_connection_config(job.connection_config)
    Orch->>Orch: ConnectorRegistry.get_connector(source_type, config)

    loop for each RawDocument from connector.fetch_documents()
        Conn-->>Orch: RawDocument(content_bytes, metadata)
        Orch->>Orch: sha256(content_bytes) → file_hash
        Orch->>PGD: get_document_by_external_id(external_id)

        alt Hash unchanged + status=INDEXED
            Orch->>Orch: skip (CDC — no change)
        else Hash changed or new document
            Orch->>PGD: CREATE or UPDATE EnterpriseDocument
            Orch->>Disk: save_staged_file(content_bytes)
            Orch->>Orch: append subtask signature to list
        end
    end

    Orch->>PGD: purge_deleted_external_documents\n(active_external_ids)
    Note over PGD,QD: Purge removes vectors from Qdrant\nand rows from PostgreSQL

    Orch->>Redis: group(subtasks).apply_async() — fan-out

    par Parallel Worker Execution
        Redis->>Workers: dequeue per-file subtasks
        Workers->>Disk: read_staged_file(staged_path)
        Workers->>QD: embed → upsert_chunks()
        Workers->>PGC: DELETE old + bulk_save_objects()
        Workers->>PGD: UPDATE status=INDEXED
        Workers->>Disk: delete_staged_file()
    end

    Orch->>PGJ: SET status=COMPLETED, last_run_at=now()
```

---

## 4. Database Ingestion Flow

```mermaid
sequenceDiagram
    participant Admin as Admin
    participant API as FastAPI\n/enterprise/jobs/:id/run
    participant Orch as Job Orchestrator
    participant Guard as SQLSecurityGuard
    participant DBConn as BaseDatabaseConnector
    participant ExtDB as External Database\n(PG/MySQL/Oracle/MSSQL)
    participant Refl as DatabaseSchemaReflector
    participant Worker as Celery Worker
    participant QD as Qdrant
    participant PGC as PostgreSQL\ndocument_chunks

    Admin->>API: POST /enterprise/jobs/{id}/run
    API->>Orch: async_execute_ingestion_job_task(job_id)

    Orch->>Guard: validate_db_target(dialect, db_url)\n[SSRF check]

    alt Mode = SCHEMA_REFLECTION
        Orch->>Refl: reflect_schema(dialect, db_url)
        Refl->>ExtDB: SQLAlchemy inspect()\nMetaData.reflect()
        ExtDB-->>Refl: table names, columns, PKs, FKs
        Refl->>ExtDB: SELECT DISTINCT col LIMIT 3\n[sample values per text column]
        ExtDB-->>Refl: sample values
        Refl->>Refl: generate_schema_documents()\n→ DDL RawDocuments per table

        loop for each schema RawDocument
            Orch->>Orch: CDC hash check
            Orch->>Worker: async_ingest_connector_document_task
            Worker->>QD: embed DDL text → upsert
            Worker->>PGC: save DocumentChunk
        end

    else Mode = ROW_EXTRACTION
        Orch->>Guard: validate_query(sql_query)\n[AST — SELECT only]
        Orch->>Guard: execute_read_only_query(engine, sql_query)
        Guard->>ExtDB: SET TRANSACTION READ ONLY\n+ statement_timeout
        Guard->>ExtDB: execute(sql_query)
        ExtDB-->>Guard: rows streamed in batches of 500

        loop for each row batch
            Guard-->>DBConn: yield row dicts
            DBConn->>DBConn: serialise row → text\n(text_column or KV pairs)
            DBConn->>DBConn: yield RawDocument per row
            Orch->>Worker: async_ingest_connector_document_task
            Worker->>QD: embed row text → upsert
            Worker->>PGC: save DocumentChunk
        end
    end
```

---

## 5. Dual-Write Consistency & Rollback

```mermaid
flowchart TD
    A[Generate embeddings for all chunks] --> B[Build PointStruct list\nBuild DocumentChunk list]
    B --> C{Qdrant upsert}
    C -- success --> D{PostgreSQL bulk_save}
    C -- failure --> E[Raise exception\nNo Qdrant data written]

    D -- success --> F[Commit\nReturn chunk_count]
    D -- failure --> G[db.rollback]
    G --> H[delete_by_filter\ndocument_id in Qdrant]
    H --> I[Raise db_err\nBoth stores clean]

    style E fill:#ff6b6b,color:#fff
    style I fill:#ff6b6b,color:#fff
    style F fill:#51cf66,color:#fff
```
