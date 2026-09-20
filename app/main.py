# app/main.py
import logging
from pathlib import Path
from contextlib import asynccontextmanager
from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from sqlalchemy import text, inspect

from core.config import settings
from core.database import Base, engine, SessionLocal
import modules.auth.domain.models  # Registers auth tables
import modules.governance.domain.models  # Registers governance tables
from app.routes import auth, chat, documents, governance, jobs, audit, views, indexes, observatory
from app.middleware.observatory import ObservatoryMiddleware
from core.telemetry import bind_backend_route_recorder

logger = logging.getLogger("app.bootstrap")

@asynccontextmanager
async def lifespan(app: FastAPI):
    """
    Conditional Database Bootstrap:
    - If Alembic is active ('alembic_version' table exists), it defers to Alembic migrations.
    - If running afresh on an uninitialized database, it enables the pgvector extension,
      creates all tables automatically via Base.metadata.create_all(), and seeds default records.
    """
    try:
        with engine.connect() as conn:
            # 1. Ensure vector extension is enabled
            try:
                conn.execute(text("CREATE EXTENSION IF NOT EXISTS vector;"))
                conn.commit()
            except Exception as ext_err:
                logger.warning(f"[DB INIT] pgvector extension check: {ext_err}")

            # 2. Check if Alembic migration tracking table exists
            inspector = inspect(engine)
            existing_tables = inspector.get_table_names()

            if "alembic_version" in existing_tables:
                logger.info("[DB INIT] Alembic tracking table detected. Deferring to Alembic migrations.")
            else:
                logger.info("[DB INIT] Fresh database detected. Creating all tables automatically...")
                Base.metadata.create_all(bind=engine)
                logger.info("[DB INIT] All database tables created successfully.")

                # 3. Seed initial default organization & persona if empty
                db = SessionLocal()
                try:
                    from modules.auth.domain.models import Organization
                    from modules.governance.domain.models import AssistantPersona

                    default_org = db.query(Organization).filter(Organization.slug == "bluesources-limited").first()
                    if not default_org:
                        default_org = Organization(
                            name="Bluesources Limited",
                            slug="bluesources-limited"
                        )
                        db.add(default_org)
                        db.commit()
                        db.refresh(default_org)
                        logger.info(f"[DB INIT] Seeded default organization: {default_org.name}")

                    default_persona = db.query(AssistantPersona).filter(
                        AssistantPersona.org_id == default_org.id,
                        AssistantPersona.is_default == True
                    ).first()
                    if not default_persona:
                        default_persona = AssistantPersona(
                            org_id=default_org.id,
                            name="Enterprise Assistant",
                            description="Default general-purpose AI assistant",
                            system_instruction_template="You are a helpful AI assistant for {{ org_name }}.",
                            temperature=0.3,
                            is_default=True,
                            is_active=True
                        )
                        db.add(default_persona)
                        db.commit()
                        logger.info("[DB INIT] Seeded default assistant persona.")
                except Exception as seed_err:
                    logger.warning(f"[DB INIT] Seeding notice: {seed_err}")
                finally:
                    db.close()

    except Exception as boot_err:
        logger.error(f"[DB INIT ERROR] Startup database initialization failed: {boot_err}")

    # 4. Initialize Observatory Telemetry & Background Hardware Sampler
    if getattr(settings, "OBSERVATORY_ENABLED", True):
        try:
            from modules.observatory.buffer import get_observatory_buffer
            from modules.observatory.sampler import get_hardware_sampler

            bind_backend_route_recorder(get_observatory_buffer().record_turn_route)
            get_hardware_sampler().start()
            logger.info("[OBSERVATORY] Telemetry buffer & background hardware sampler initialized.")
        except Exception as obs_err:
            logger.warning(f"[OBSERVATORY] Startup initialization notice: {obs_err}")

    yield

    if getattr(settings, "OBSERVATORY_ENABLED", True):
        try:
            from modules.observatory.sampler import get_hardware_sampler

            get_hardware_sampler().stop()
            bind_backend_route_recorder(None)
            logger.info("[OBSERVATORY] Background hardware sampler stopped.")
        except Exception as obs_err:
            logger.debug(f"[OBSERVATORY] Shutdown notice: {obs_err}")

app = FastAPI(
    title=settings.APP_NAME,
    version=settings.APP_VERSION,
    description="Enterprise-grade Unified Cognitive RAG Platform with Multi-Source Retrieval, Dynamic RBAC, and Grounding Guardrails.",
    lifespan=lifespan
)

# Static Files Directory
BASE_DIR = Path(__file__).resolve().parent
STATIC_DIR = BASE_DIR / "static"
STATIC_DIR.mkdir(parents=True, exist_ok=True)
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# Middleware
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_headers=["*"],
    allow_methods=["*"],
)
app.add_middleware(ObservatoryMiddleware)

# Register API Routers
app.include_router(auth.router)
app.include_router(chat.router)
app.include_router(documents.router)
app.include_router(governance.router)
app.include_router(jobs.router)
app.include_router(audit.router)
app.include_router(indexes.router)
app.include_router(observatory.router)

# Register UI View Routers
app.include_router(views.router)

@app.get("/health")
def health_check():
    return {
        "status": "healthy",
        "app": settings.APP_NAME,
        "version": settings.APP_VERSION,
        "llm_provider": settings.LLM_PROVIDER
    }



# # app/main.py
# from pathlib import Path
# from fastapi import FastAPI
# from fastapi.middleware.cors import CORSMiddleware
# from fastapi.staticfiles import StaticFiles

# from core.config import settings
# from app.routes import auth, chat, documents, governance, jobs, audit, views, indexes

# app = FastAPI(
#     title=settings.APP_NAME,
#     version=settings.APP_VERSION,
#     description="Enterprise-grade Unified Cognitive RAG Platform with Multi-Source Retrieval, Dynamic RBAC, and Grounding Guardrails."
# )

# # Static Files Directory
# BASE_DIR = Path(__file__).resolve().parent
# STATIC_DIR = BASE_DIR / "static"
# STATIC_DIR.mkdir(parents=True, exist_ok=True)
# app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

# # Middleware
# app.add_middleware(
#     CORSMiddleware,
#     allow_origins=["*"],
#     allow_credentials=True,
#     allow_headers=["*"],
#     allow_methods=["*"],
# )

# # Register API Routers first (ensures specific API paths are evaluated before view wildcards)
# app.include_router(auth.router)
# app.include_router(chat.router)
# app.include_router(documents.router)
# app.include_router(governance.router)
# app.include_router(jobs.router)
# app.include_router(audit.router)
# app.include_router(indexes.router)

# # Register UI View Routers last
# app.include_router(views.router)

# @app.get("/health")
# def health_check():
#     return {
#         "status": "healthy",
#         "app": settings.APP_NAME,
#         "version": settings.APP_VERSION,
#         "llm_provider": settings.LLM_PROVIDER
#     }
