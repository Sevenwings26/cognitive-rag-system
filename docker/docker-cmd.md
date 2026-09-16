# Docker Commands Guide: Multi-Tenant Enterprise RAG Stack

This guide details how to run the multi-tenant RAG architecture sequentially using the decoupled compose configuration.

- **Storage Infrastructure:** `docker-compose.storage.yml` (PostgreSQL + pgvector, Qdrant vector DB, Redis broker)
- **Application & Worker Services:** `docker-compose.yml` (FastAPI Retrieval AI, Celery Ingestion Worker)
- **Shared Network:** `rag_network` (Ensures seamless cross-service communication by container service name)

---

## 1. Quick Start: Sequential Stack Execution

Execute the following commands from the `docker/` directory:

```bash
cd /home/techyz-admin/sevenwings/03_learning/26-08-10-ai-system/multi-tenant-rag-system/docker
```

### Step 1: Start the Storage Layer (Postgres, Qdrant, Redis)
Start the foundational databases and message broker first:
```bash
docker compose -f docker-compose.storage.yml up -d
```

### Step 2: Verify Storage Health
Ensure PostgreSQL has passed its health check and is accepting connections:
```bash
# Check container status (wait until postgres_db shows 'healthy')
docker compose -f docker-compose.storage.yml ps

# Direct PostgreSQL readiness check
docker compose -f docker-compose.storage.yml exec postgres pg_isready -U postgres -d wings_db
```

### Step 3: Run Database Migrations (Alembic)
Once the database is healthy, run database schema migrations before bringing up worker/API services:
```bash
# Option A: Run migration directly using an ephemeral container
docker compose run --rm wings_retrival_ai alembic upgrade head
```

### Step 4: Start Application & Worker Services
Once storage is ready and healthy, start the FastAPI API (`wings_retrival_ai`) and Celery worker (`wings_ingestion_worker`):
```bash
# Start application and worker services
docker compose up -d wings_retrival_ai wings_ingestion_worker

# OR start full stack (automatically attaches to healthy storage)
docker compose up -d
```

---

## 2. Service Management & Operations

### Viewing Live Logs
```bash
# Follow logs for all services
docker compose logs -f

# Follow storage services only
docker compose -f docker-compose.storage.yml logs -f

# Follow individual application/worker services
docker compose logs -f wings_retrival_ai
docker compose logs -f wings_ingestion_worker
```

### Checking Status
```bash
# List all running containers in the stack
docker compose ps
```

### Rebuilding Application Containers
When dependencies in `requirements.txt` or system packages in `Dockerfile` change:
```bash
# Rebuild without cache
docker compose build --no-cache wings_retrival_ai wings_ingestion_worker

# Restart application services with newly built images
docker compose up -d --build wings_retrival_ai wings_ingestion_worker
```

### Database & Migration Management
```bash
# Generate a new migration script
docker compose exec wings_retrival_ai alembic revision --autogenerate -m "describe_changes"

# Apply pending migrations
docker compose exec wings_retrival_ai alembic upgrade head

# Terminate stuck backend connections (if needed)
docker compose -f docker-compose.storage.yml exec postgres psql -U postgres -d wings_db -c "SELECT pg_terminate_backend(pid) FROM pg_stat_activity WHERE datname = 'wings_db' AND pid <> pg_backend_pid();"
```

### Interactive Shell & Testing
```bash
# Open interactive bash shell inside FastAPI container
docker compose exec -it wings_retrival_ai /bin/bash

# Execute verification or benchmark script
docker compose exec wings_retrival_ai python benchmarking/test_verification.py
```

---

## 3. Stopping the Stack

### Stop Application & Worker Only (Keep Databases Running)
Ideal for local Python/debugging workflows where you want storage available:
```bash
docker compose stop wings_retrival_ai wings_ingestion_worker
```

### Stop Storage Only
```bash
docker compose -f docker-compose.storage.yml stop
```

### Stop and Remove Entire Stack
```bash
# Stop all containers in the multi-tenant-rag stack
docker compose down

# Stop all containers and remove persistent storage volumes (CAUTION: wipes database & vectors)
docker compose down -v
```

---

## 4. Running from the Project Root (Alternative)

If you prefer running Docker commands from the root `multi-tenant-rag-system/` directory instead of changing into `docker/`:

```bash
# Start storage layer
docker compose -f docker/docker-compose.storage.yml up -d

# Start app & worker
docker compose -f docker/docker-compose.yml up -d

# Check status
docker compose -f docker/docker-compose.yml ps

# Stop stack
docker compose -f docker/docker-compose.yml down
```
