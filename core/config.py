# app/core/config.py
import os
from typing import Optional
from dotenv import load_dotenv

# Load .env file
load_dotenv()

try:
    from pydantic_settings import BaseSettings, SettingsConfigDict
    class Settings(BaseSettings):
        APP_NAME: str = "Enterprise Multi-Tenant AI & RAG Platform"
        APP_VERSION: str = "2.0.0"
        DEBUG: bool = False

        DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://postgres:Password#123@host.docker.internal:5432/wings_orgs")
        ENTERPRISE_SECRET_KEY: str = os.getenv("ENTERPRISE_SECRET_KEY", "enterprise-secure-jwt-secret-key-production-32-chars")
        ALGORITHM: str = "HS256"
        ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 60 * 24))

        LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "ollama")
        GEMINI_API_KEY: Optional[str] = os.getenv("GEMINI_API_KEY")
        GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

        VLLM_BASE_URL: str = os.getenv("VLLM_BASE_URL", "http://host.docker.internal:8000/v1")
        VLLM_API_KEY: str = os.getenv("VLLM_API_KEY", "not-needed")
        VLLM_MODEL: Optional[str] = os.getenv("VLLM_MODEL", "Qwen/Qwen2.5-3B-Instruct-AWQ")
        VLLM_EMBEDDING_MODEL: Optional[str] = os.getenv("VLLM_EMBEDDING_MODEL")

        OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434/v1")
        OLLAMA_API_KEY: str = os.getenv("OLLAMA_API_KEY", "ollama")
        OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "gemma3:12b")
        OLLAMA_EMBEDDING_MODEL: str = os.getenv("OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:0.6b")

        OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
        OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        OPENAI_EMBEDDING_MODEL: str = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

        QDRANT_STORAGE: str = os.getenv("QDRANT_STORAGE", "server")
        QDRANT_HOST: str = os.getenv("QDRANT_HOST", "qdrant")
        QDRANT_PORT: int = int(os.getenv("QDRANT_PORT", 6333))
        QDRANT_API_KEY: Optional[str] = os.getenv("QDRANT_API_KEY")
        QDRANT_PATH: str = os.getenv("QDRANT_PATH", "./qdrant_db")
        QDRANT_COLLECTION: str = os.getenv("QDRANT_COLLECTION", "assistant_knowledge")
        EMBEDDING_DIMENSION: int = int(os.getenv("EMBEDDING_DIMENSION", 1024))

        REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379/0")
        STORAGE_STAGING_DIR: str = os.getenv("STORAGE_STAGING_DIR", "/app/storage/staging")

        DEFAULT_SCORE_THRESHOLD: float = 0.35
        DEFAULT_TOP_K: int = 3
        DEFAULT_CANDIDATE_LIMIT: int = 15
        RERANKER_MODEL: str = "ms-marco-TinyBERT-L-2-v2"

        # Database Security & Read-Only Sandbox Settings
        DB_ENCRYPTION_KEY: Optional[str] = os.getenv("DB_ENCRYPTION_KEY")
        EXTERNAL_DB_STATEMENT_TIMEOUT_MS: int = int(os.getenv("EXTERNAL_DB_STATEMENT_TIMEOUT_MS", 15000))
        EXTERNAL_DB_CONNECT_TIMEOUT_SECONDS: int = int(os.getenv("EXTERNAL_DB_CONNECT_TIMEOUT_SECONDS", 5))
        EXTERNAL_DB_FETCH_BATCH_SIZE: int = int(os.getenv("EXTERNAL_DB_FETCH_BATCH_SIZE", 500))
        ALLOWED_DB_PRIVATE_HOSTS: str = os.getenv("ALLOWED_DB_PRIVATE_HOSTS", "localhost,127.0.0.1,host.docker.internal,postgres,mysql,oracle,mssql")
        ALLOWED_DB_PRIVATE_CIDRS: str = os.getenv("ALLOWED_DB_PRIVATE_CIDRS", "")

        model_config = SettingsConfigDict(env_file=".env", env_file_encoding="utf-8", extra="ignore")

except ImportError:
    from pydantic import BaseModel
    class Settings(BaseModel):
        APP_NAME: str = "Enterprise Multi-Tenant AI & RAG Platform"
        APP_VERSION: str = "2.0.0"
        DEBUG: bool = False

        DATABASE_URL: str = os.getenv("DATABASE_URL", "postgresql://postgres:Password#123@host.docker.internal:5432/wings_orgs")
        ENTERPRISE_SECRET_KEY: str = os.getenv("ENTERPRISE_SECRET_KEY", "enterprise-secure-jwt-secret-key-production-32-chars")
        ALGORITHM: str = "HS256"
        ACCESS_TOKEN_EXPIRE_MINUTES: int = int(os.getenv("ACCESS_TOKEN_EXPIRE_MINUTES", 60 * 24))

        LLM_PROVIDER: str = os.getenv("LLM_PROVIDER", "ollama")
        GEMINI_API_KEY: Optional[str] = os.getenv("GEMINI_API_KEY")
        GEMINI_MODEL: str = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")

        VLLM_BASE_URL: str = os.getenv("VLLM_BASE_URL", "http://host.docker.internal:8000/v1")
        VLLM_API_KEY: str = os.getenv("VLLM_API_KEY", "not-needed")
        VLLM_MODEL: Optional[str] = os.getenv("VLLM_MODEL", "Qwen/Qwen2.5-3B-Instruct-AWQ")
        VLLM_EMBEDDING_MODEL: Optional[str] = os.getenv("VLLM_EMBEDDING_MODEL")

        OLLAMA_BASE_URL: str = os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434/v1")
        OLLAMA_API_KEY: str = os.getenv("OLLAMA_API_KEY", "ollama")
        OLLAMA_MODEL: str = os.getenv("OLLAMA_MODEL", "gemma3:12b")
        OLLAMA_EMBEDDING_MODEL: str = os.getenv("OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:0.6b")

        OPENAI_API_KEY: Optional[str] = os.getenv("OPENAI_API_KEY")
        OPENAI_MODEL: str = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
        OPENAI_EMBEDDING_MODEL: str = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")

        QDRANT_STORAGE: str = os.getenv("QDRANT_STORAGE", "server")
        QDRANT_HOST: str = os.getenv("QDRANT_HOST", "qdrant")
        QDRANT_PORT: int = int(os.getenv("QDRANT_PORT", 6333))
        QDRANT_API_KEY: Optional[str] = os.getenv("QDRANT_API_KEY")
        QDRANT_PATH: str = os.getenv("QDRANT_PATH", "./qdrant_db")
        QDRANT_COLLECTION: str = os.getenv("QDRANT_COLLECTION", "assistant_knowledge")
        EMBEDDING_DIMENSION: int = int(os.getenv("EMBEDDING_DIMENSION", 1024))

        REDIS_URL: str = os.getenv("REDIS_URL", "redis://redis:6379/0")
        STORAGE_STAGING_DIR: str = os.getenv("STORAGE_STAGING_DIR", "/app/storage/staging")

        DEFAULT_SCORE_THRESHOLD: float = 0.35
        DEFAULT_TOP_K: int = 3
        DEFAULT_CANDIDATE_LIMIT: int = 15
        RERANKER_MODEL: str = "ms-marco-TinyBERT-L-2-v2"

        # Database Security & Read-Only Sandbox Settings
        DB_ENCRYPTION_KEY: Optional[str] = os.getenv("DB_ENCRYPTION_KEY")
        EXTERNAL_DB_STATEMENT_TIMEOUT_MS: int = int(os.getenv("EXTERNAL_DB_STATEMENT_TIMEOUT_MS", 15000))
        EXTERNAL_DB_CONNECT_TIMEOUT_SECONDS: int = int(os.getenv("EXTERNAL_DB_CONNECT_TIMEOUT_SECONDS", 5))
        EXTERNAL_DB_FETCH_BATCH_SIZE: int = int(os.getenv("EXTERNAL_DB_FETCH_BATCH_SIZE", 500))
        ALLOWED_DB_PRIVATE_HOSTS: str = os.getenv("ALLOWED_DB_PRIVATE_HOSTS", "localhost,127.0.0.1,host.docker.internal,postgres,mysql,oracle,mssql")
        ALLOWED_DB_PRIVATE_CIDRS: str = os.getenv("ALLOWED_DB_PRIVATE_CIDRS", "")

settings = Settings()
