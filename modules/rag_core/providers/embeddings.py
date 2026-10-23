# modules/rag_core/providers/embeddings.py
"""
Standalone Embedding Provider Layer.
Decouples vector representation generation from LLM text generation/reasoning.
Supports Ollama, OpenAI-compatible servers (vLLM, TEI, LocalAI), OpenAI, and Google Gemini.
"""
import os
import logging
from abc import ABC, abstractmethod
from typing import List, Optional

logger = logging.getLogger("embedding_provider")


class BaseEmbeddingService(ABC):
    """Abstract Base Class for all standalone embedding providers."""

    @abstractmethod
    def get_embeddings(self, text: str) -> List[float]:
        """Generate a vector embedding for a single text chunk or query."""
        pass

    def get_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        """
        Generate vector embeddings for multiple text chunks.
        Default fallback generates sequentially, while subclasses can implement native batching.
        """
        return [self.get_embeddings(t) for t in texts]


class OpenAICompatibleEmbeddingService(BaseEmbeddingService):
    """
    Embedding provider for OpenAI-compatible endpoints:
    Ollama, vLLM, Text Embeddings Inference (TEI), LocalAI, or official OpenAI.
    """
    def __init__(
        self,
        base_url: Optional[str],
        api_key: Optional[str] = None,
        model_name: Optional[str] = None
    ):
        from openai import OpenAI
        self.base_url = base_url
        self.api_key = api_key or "not-needed"
        self.model_name = model_name or "qwen3-embedding:0.6b"
        self.client = OpenAI(base_url=self.base_url, api_key=self.api_key)

    def get_embeddings(self, text: str) -> List[float]:
        try:
            response = self.client.embeddings.create(
                model=self.model_name,
                input=text
            )
            return response.data[0].embedding
        except Exception as e:
            logger.error(f"Failed to generate embeddings from {self.base_url} ({self.model_name}): {e}")
            raise RuntimeError(f"Embedding generation failure: {e}")

    def get_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        try:
            response = self.client.embeddings.create(
                model=self.model_name,
                input=texts
            )
            sorted_data = sorted(response.data, key=lambda x: getattr(x, "index", 0))
            return [d.embedding for d in sorted_data]
        except Exception as e:
            logger.warning(f"Batch embedding failed ({e}); falling back to sequential generation.")
            return [self.get_embeddings(t) for t in texts]


class GeminiEmbeddingService(BaseEmbeddingService):
    """
    Embedding provider for Google Gemini GenAI SDK.
    """
    def __init__(self, api_key: Optional[str] = None, model_name: str = "text-embedding-004"):
        self.api_key = api_key or os.getenv("GEMINI_API_KEY")
        self.model_name = model_name
        self._init_client()

    def _init_client(self):
        try:
            from google import genai
            self.client = genai.Client(api_key=self.api_key)
            self._use_new_sdk = True
        except (ImportError, AttributeError):
            import google.generativeai as legacy_genai
            legacy_genai.configure(api_key=self.api_key)
            self.legacy_client = legacy_genai
            self._use_new_sdk = False

    def get_embeddings(self, text: str) -> List[float]:
        try:
            if self._use_new_sdk:
                response = self.client.models.embed_content(
                    model=self.model_name,
                    contents=text
                )
                return response.embeddings[0].values
            else:
                resolved_model = self.model_name if "models/" in self.model_name else f"models/{self.model_name}"
                result = self.legacy_client.embed_content(
                    model=resolved_model,
                    content=text,
                    task_type="retrieval_document"
                )
                return result["embedding"]
        except Exception as e:
            logger.error(f"Failed to generate Gemini embeddings ({self.model_name}): {e}")
            raise RuntimeError(f"Gemini embedding failure: {e}")

    def get_embeddings_batch(self, texts: List[str]) -> List[List[float]]:
        if not texts:
            return []
        try:
            if self._use_new_sdk:
                response = self.client.models.embed_content(
                    model=self.model_name,
                    contents=texts
                )
                return [emb.values for emb in response.embeddings]
            else:
                return [self.get_embeddings(t) for t in texts]
        except Exception as e:
            logger.warning(f"Batch Gemini embedding failed ({e}); falling back to sequential.")
            return [self.get_embeddings(t) for t in texts]


class EmbeddingFactory:
    """
    Factory for instantiating the standalone embedding service.
    Decoupled from LLM_PROVIDER.
    """
    _instance: Optional[BaseEmbeddingService] = None

    @classmethod
    def get_provider(cls, force_reload: bool = False) -> BaseEmbeddingService:
        if cls._instance is not None and not force_reload:
            return cls._instance

        from core.config import settings

        # Priority: EMBEDDING_PROVIDER -> fallback to OLLAMA or LLM_PROVIDER
        provider = (
            os.getenv("EMBEDDING_PROVIDER") or
            getattr(settings, "EMBEDDING_PROVIDER", None) or
            "ollama"
        ).lower()

        # Resolve Model Name: EMBEDDING_MODEL -> provider-specific fallback
        model = (
            os.getenv("EMBEDDING_MODEL") or
            getattr(settings, "EMBEDDING_MODEL", None)
        )

        if provider == "ollama":
            base_url = (
                getattr(settings, "EMBEDDING_BASE_URL", None) or
                os.getenv("EMBEDDING_BASE_URL") or
                getattr(settings, "OLLAMA_BASE_URL", None) or
                os.getenv("OLLAMA_BASE_URL", "http://host.docker.internal:11434/v1")
            )
            api_key = (
                getattr(settings, "EMBEDDING_API_KEY", None) or
                os.getenv("EMBEDDING_API_KEY") or
                getattr(settings, "OLLAMA_API_KEY", None) or
                os.getenv("OLLAMA_API_KEY", "ollama")
            )
            model_name = model or getattr(settings, "OLLAMA_EMBEDDING_MODEL", "qwen3-embedding:0.6b")
            cls._instance = OpenAICompatibleEmbeddingService(
                base_url=base_url,
                api_key=api_key,
                model_name=model_name
            )

        elif provider in ("openai", "openai_compatible"):
            base_url = (
                getattr(settings, "EMBEDDING_BASE_URL", None) or
                os.getenv("EMBEDDING_BASE_URL") or
                None
            )
            api_key = (
                getattr(settings, "EMBEDDING_API_KEY", None) or
                os.getenv("EMBEDDING_API_KEY") or
                getattr(settings, "OPENAI_API_KEY", None) or
                os.getenv("OPENAI_API_KEY", "")
            )
            model_name = model or getattr(settings, "OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
            cls._instance = OpenAICompatibleEmbeddingService(
                base_url=base_url,
                api_key=api_key,
                model_name=model_name
            )

        elif provider == "vllm":
            base_url = (
                getattr(settings, "EMBEDDING_BASE_URL", None) or
                os.getenv("EMBEDDING_BASE_URL") or
                getattr(settings, "VLLM_BASE_URL", None) or
                os.getenv("VLLM_BASE_URL", "http://host.docker.internal:8000/v1")
            )
            api_key = (
                getattr(settings, "EMBEDDING_API_KEY", None) or
                os.getenv("EMBEDDING_API_KEY") or
                getattr(settings, "VLLM_API_KEY", None) or
                os.getenv("VLLM_API_KEY", "not-needed")
            )
            model_name = model or getattr(settings, "VLLM_EMBEDDING_MODEL", "your-local-embedding-model-name")
            cls._instance = OpenAICompatibleEmbeddingService(
                base_url=base_url,
                api_key=api_key,
                model_name=model_name
            )

        elif provider == "gemini":
            api_key = (
                getattr(settings, "EMBEDDING_API_KEY", None) or
                os.getenv("EMBEDDING_API_KEY") or
                getattr(settings, "GEMINI_API_KEY", None) or
                os.getenv("GEMINI_API_KEY")
            )
            model_name = model or getattr(settings, "GEMINI_EMBEDDING_MODEL", None) or os.getenv("GEMINI_EMBEDDING_MODEL", "text-embedding-004")
            cls._instance = GeminiEmbeddingService(
                api_key=api_key,
                model_name=model_name
            )

        else:
            logger.warning(f"Unknown EMBEDDING_PROVIDER '{provider}', falling back to Ollama OpenAICompatibleEmbeddingService.")
            cls._instance = OpenAICompatibleEmbeddingService(
                base_url=os.getenv("EMBEDDING_BASE_URL", "http://host.docker.internal:11434/v1"),
                api_key=os.getenv("EMBEDDING_API_KEY", "ollama"),
                model_name=model or "qwen3-embedding:0.6b"
            )

        return cls._instance
