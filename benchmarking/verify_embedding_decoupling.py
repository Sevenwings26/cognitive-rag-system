import os
import sys

# Ensure project root is in sys.path
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

from core.config import settings
from modules.rag_core.providers.llm import LLMFactory
from modules.rag_core.providers.embeddings import EmbeddingFactory
from modules.rag_core.orchestrator.unified_orchestrator import UnifiedRAGOrchestrator
from app.dependencies import get_orchestrator

def verify():
    print(f"[CONFIG] settings.LLM_PROVIDER: {settings.LLM_PROVIDER}")
    print(f"[CONFIG] settings.EMBEDDING_PROVIDER: {settings.EMBEDDING_PROVIDER}")
    print(f"[CONFIG] settings.OLLAMA_EMBEDDING_MODEL: {settings.OLLAMA_EMBEDDING_MODEL}")
    print(f"[CONFIG] settings.EMBEDDING_DIMENSION: {settings.EMBEDDING_DIMENSION}")

    # 1. LLM Provider
    llm = LLMFactory.get_provider()
    print(f"\n[LLM FACTORY] Type: {type(llm).__name__}, Model: {getattr(llm, 'model_name', 'N/A')}")
    assert "GeminiService" in type(llm).__name__, f"Expected GeminiService, got {type(llm).__name__}"

    # 2. Embedding Provider
    emb = EmbeddingFactory.get_provider()
    print(f"[EMBEDDING FACTORY] Type: {type(emb).__name__}, Model: {emb.model_name}, Base URL: {emb.base_url}")
    assert emb.model_name == "qwen3-embedding:0.6b", f"Expected qwen3-embedding:0.6b, got {emb.model_name}"

    # 3. Dependency Injection: get_orchestrator()
    orch = get_orchestrator()
    print(f"\n[ORCHESTRATOR] LLM: {type(orch.llm).__name__} ({orch.llm.model_name})")
    print(f"[ORCHESTRATOR] Embedding: {type(orch.embedding_service).__name__} ({orch.embedding_service.model_name})")
    assert orch.llm.model_name == "gemini-2.5-flash"
    assert orch.embedding_service.model_name == "qwen3-embedding:0.6b"

    # 4. Strategy Decoupling Verification
    print(f"[HYBRID RETRIEVER] Embedding: {orch.hybrid_retriever.embedding_service.model_name}")
    print(f"[SESSION STRATEGY] Embedding: {orch.session_strategy.embedding_service.model_name}")
    print(f"[ENTERPRISE STRATEGY] Retriever Embedding: {orch.enterprise_strategy.hybrid_retriever.embedding_service.model_name}")
    print(f"[SQL STRATEGY] Embedding: {orch.sql_strategy.embedding_service.model_name}")
    assert orch.hybrid_retriever.embedding_service.model_name == "qwen3-embedding:0.6b"
    assert orch.session_strategy.embedding_service.model_name == "qwen3-embedding:0.6b"
    assert orch.enterprise_strategy.hybrid_retriever.embedding_service.model_name == "qwen3-embedding:0.6b"
    assert orch.sql_strategy.embedding_service.model_name == "qwen3-embedding:0.6b"

    # 5. Celery Worker get_services()
    try:
        from modules.tasks.workers.ingestion_tasks import get_services
        db, worker_orch = get_services()
        print(f"\n[CELERY WORKER] LLM: {type(worker_orch.llm).__name__} ({worker_orch.llm.model_name})")
        print(f"[CELERY WORKER] Embedding: {type(worker_orch.embedding_service).__name__} ({worker_orch.embedding_service.model_name})")
        assert worker_orch.embedding_service.model_name == "qwen3-embedding:0.6b"
        db.close()
    except ImportError as e:
        print(f"\n[CELERY WORKER] Skipped (celery not installed in host virtualenv, runs inside container: {e})")

    # 6. Test Switching Capabilities
    print("\n--- Testing Dynamic Provider Switching Matrix ---")
    # Case A: Switch LLM to Ollama while keeping Embedding on Ollama
    os.environ["LLM_PROVIDER"] = "ollama"
    llm_ollama = LLMFactory.get_provider()
    emb_ollama = EmbeddingFactory.get_provider(force_reload=True)
    print(f"Case A (LLM=ollama, EMB=ollama) -> LLM: {llm_ollama.model_name}, EMB: {emb_ollama.model_name}")
    assert llm_ollama.model_name == "gemma3:12b"
    assert emb_ollama.model_name == "qwen3-embedding:0.6b"

    # Case B: Switch LLM to Gemini, Embedding to Gemini (explicit EMBEDDING_MODEL=text-embedding-004)
    os.environ["LLM_PROVIDER"] = "gemini"
    os.environ["EMBEDDING_PROVIDER"] = "gemini"
    os.environ["EMBEDDING_MODEL"] = "text-embedding-004"
    llm_gemini = LLMFactory.get_provider()
    emb_gemini = EmbeddingFactory.get_provider(force_reload=True)
    print(f"Case B (LLM=gemini, EMB=gemini) -> LLM: {llm_gemini.model_name}, EMB: {emb_gemini.model_name}")
    assert llm_gemini.model_name == "gemini-2.5-flash"
    assert emb_gemini.model_name == "text-embedding-004"

    # Case B2: Switch to Gemini with no EMBEDDING_MODEL set (falls back to provider default text-embedding-004)
    old_setting_model = getattr(settings, "EMBEDDING_MODEL", None)
    settings.EMBEDDING_MODEL = None
    os.environ.pop("EMBEDDING_MODEL", None)
    emb_gemini_default = EmbeddingFactory.get_provider(force_reload=True)
    print(f"Case B2 (EMB=gemini, default fallback) -> EMB: {emb_gemini_default.model_name}")
    assert emb_gemini_default.model_name == "text-embedding-004"
    settings.EMBEDDING_MODEL = old_setting_model

    # Case C: Switch LLM to Gemini, Embedding back to Ollama (The exact target scenario)
    os.environ["LLM_PROVIDER"] = "gemini"
    os.environ["EMBEDDING_PROVIDER"] = "ollama"
    os.environ["EMBEDDING_MODEL"] = "qwen3-embedding:0.6b"
    llm_target = LLMFactory.get_provider()
    emb_target = EmbeddingFactory.get_provider(force_reload=True)
    print(f"Case C (LLM=gemini, EMB=ollama) -> LLM: {llm_target.model_name}, EMB: {emb_target.model_name}")
    assert llm_target.model_name == "gemini-2.5-flash"
    assert emb_target.model_name == "qwen3-embedding:0.6b"

    # Case D: Custom EMBEDDING_MODEL on Ollama
    os.environ["EMBEDDING_PROVIDER"] = "ollama"
    os.environ["EMBEDDING_MODEL"] = "nomic-embed-text"
    emb_custom = EmbeddingFactory.get_provider(force_reload=True)
    print(f"Case D (EMBEDDING_MODEL override on Ollama) -> Model: {emb_custom.model_name}")
    assert emb_custom.model_name == "nomic-embed-text"

    # Case E: Custom EMBEDDING_MODEL on vLLM
    os.environ["EMBEDDING_PROVIDER"] = "vllm"
    os.environ["EMBEDDING_MODEL"] = "BAAI/bge-m3"
    emb_vllm = EmbeddingFactory.get_provider(force_reload=True)
    print(f"Case E (EMBEDDING_MODEL override on vLLM) -> Model: {emb_vllm.model_name}")
    assert emb_vllm.model_name == "BAAI/bge-m3"

    # Case F: Custom EMBEDDING_MODEL on Gemini
    os.environ["EMBEDDING_PROVIDER"] = "gemini"
    os.environ["EMBEDDING_MODEL"] = "text-embedding-004"
    emb_gem = EmbeddingFactory.get_provider(force_reload=True)
    print(f"Case F (EMBEDDING_MODEL override on Gemini) -> Model: {emb_gem.model_name}")
    assert emb_gem.model_name == "text-embedding-004"

    # Reset environment back to default
    os.environ["LLM_PROVIDER"] = "gemini"
    os.environ["EMBEDDING_PROVIDER"] = "ollama"
    os.environ["EMBEDDING_MODEL"] = "qwen3-embedding:0.6b"

    print("\n>>> ALL VERIFICATION CHECKS & SWITCHING TESTS PASSED SUCCESSFULLY! Decoupling is 100% active. <<<")

if __name__ == "__main__":
    verify()
