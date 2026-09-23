# core/database.py
import os
from typing import Generator
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker, declarative_base, Session
from core.config import settings

DATABASE_URL = settings.DATABASE_URL or os.getenv("DATABASE_URL")
if DATABASE_URL and DATABASE_URL.startswith("postgresql+psycopg://"):
    try:
        import psycopg
    except ImportError:
        DATABASE_URL = DATABASE_URL.replace("postgresql+psycopg://", "postgresql+psycopg2://", 1)

engine = create_engine(DATABASE_URL)
SessionLocal = sessionmaker(autocommit=False, autoflush=False, bind=engine)
Base = declarative_base()

def get_db() -> Generator[Session, None, None]:
    """Dependency helper yielding a scoped database session."""
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()

