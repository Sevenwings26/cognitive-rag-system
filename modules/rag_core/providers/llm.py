# app/services/llm.py
"""
Using the Provider Pattern (also known as the Strategy Pattern) to which between inference engines - Google Gemini, vLLM, OpenAI, Ollama
"""
import os
from abc import ABC, abstractmethod
from typing import Optional, List, Iterator
from google.genai import types
from google import genai


# Define the Abstract Base Class (Interface)
class BaseLLMService(ABC):
    """
    Abstract Base Class defining the interface for all LLM providers.
    Any provider (Gemini, vLLM, OpenAI, Ollama) must implement these methods.
    """
    
    @abstractmethod
    def generate_text(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> str:
        """
        Generate text content from a prompt.
        """
        pass

    def stream_text(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> Iterator[str]:
        """
        Stream text content incrementally from a prompt.
        Default fallback yields the complete generated text in a single chunk.
        """
        text = self.generate_text(prompt, system_instruction=system_instruction, **kwargs)
        if text:
            yield text

    @abstractmethod
    def get_embeddings(self, text: str) -> List[float]:
        """
        Generate vector embeddings for a given text.
        """
        pass


# Implementation 1: Gemini Provider (New Google GenAI SDK)
# try:

class GeminiService(BaseLLMService):
    def __init__(self, api_key: str, model_name: str = "gemini-2.5-flash", embedding_model: str = "text-embedding-004"):
        self.client = genai.Client(api_key=api_key)
        self.model_name = model_name
        self.embedding_model = embedding_model

    def generate_text(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> str:
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=kwargs.get("temperature", 0.2),
            max_output_tokens=kwargs.get("max_tokens", None)
        )
        response = self.client.models.generate_content(
            model=self.model_name,
            contents=prompt,
            config=config
        )
        return response.text.strip()

    def stream_text(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> Iterator[str]:
        config = types.GenerateContentConfig(
            system_instruction=system_instruction,
            temperature=kwargs.get("temperature", 0.2),
            max_output_tokens=kwargs.get("max_tokens", None)
        )
        try:
            for chunk in self.client.models.generate_content_stream(
                model=self.model_name,
                contents=prompt,
                config=config
            ):
                if chunk.text:
                    yield chunk.text
        except Exception:
            yield self.generate_text(prompt, system_instruction=system_instruction, **kwargs)

    def get_embeddings(self, text: str) -> List[float]:
        response = self.client.models.embed_content(
            model=self.embedding_model,
            contents=text
        )
        return response.embeddings[0].values
            
# except ImportError:
#     # Fallback to Legacy google-generativeai SDK if the new SDK is not installed yet
#     import google.generativeai as genai
#     class GeminiService(BaseLLMService):
#         def __init__(self, api_key: str, model_name: str = "gemini-2.5-flash", embedding_model: str = "models/text-embedding-004"):
#             genai.configure(api_key=api_key)
#             self.model_name = model_name
#             self.embedding_model = embedding_model

#         def generate_text(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> str:
#             model = genai.GenerativeModel(
#                 model_name=self.model_name,
#                 system_instruction=system_instruction
#             )
#             response = model.generate_content(
#                 prompt,
#                 generation_config={"temperature": kwargs.get("temperature", 0.2)}
#             )
#             return response.text.strip()

#         # CORRECTED LEGACY EMBEDDING CODE:
#         def get_embeddings(self, text: str) -> List[float]:
#             result = genai.embed_content(
#                 model=self.embedding_model,
#                 content=text,
#                 task_type="retrieval_document"
#             )
#             return result['embedding']

# Implementation 2: OpenAI-Compatible Provider (vLLM, Ollama, OpenAI)
from openai import OpenAI

class OpenAICompatibleService(BaseLLMService):
    """
    Service provider for any OpenAI-compatible API endpoint (e.g. OpenAI, vLLM, Ollama).
    """
    def __init__(self, base_url: Optional[str], api_key: str, model_name: str, embedding_model: Optional[str] = None):
        # Setting base_url to None defaults to standard OpenAI production endpoint
        self.client = OpenAI(base_url=base_url, api_key=api_key)
        self.model_name = model_name
        self.embedding_model = embedding_model or model_name

    def generate_text(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> str:
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})

        response = self.client.chat.completions.create(
            model=self.model_name,
            messages=messages,
            temperature=kwargs.get("temperature", 0.2),
            max_tokens=kwargs.get("max_tokens", None)
        )
        return response.choices[0].message.content.strip()

    def stream_text(self, prompt: str, system_instruction: Optional[str] = None, **kwargs) -> Iterator[str]:
        messages = []
        if system_instruction:
            messages.append({"role": "system", "content": system_instruction})
        messages.append({"role": "user", "content": prompt})

        try:
            stream = self.client.chat.completions.create(
                model=self.model_name,
                messages=messages,
                temperature=kwargs.get("temperature", 0.2),
                max_tokens=kwargs.get("max_tokens", None),
                stream=True
            )
            for chunk in stream:
                if chunk.choices and chunk.choices[0].delta and chunk.choices[0].delta.content:
                    yield chunk.choices[0].delta.content
        except Exception:
            yield self.generate_text(prompt, system_instruction=system_instruction, **kwargs)

    def get_embeddings(self, text: str) -> List[float]:
        # If you haven't configured an embedding model for your local setup yet,
        # this will raise a clean error instead of breaking silently.
        if not self.embedding_model:
            raise NotImplementedError(
                "Embeddings are not configured for this provider. "
                "Please set VLLM_EMBEDDING_MODEL or OLLAMA_EMBEDDING_MODEL in your .env."
            )
            
        try:
            response = self.client.embeddings.create(
                model=self.embedding_model,
                input=text
            )
            return response.data[0].embedding
        except Exception as e:
            raise RuntimeError(f"Failed to generate embeddings from local server: {e}")


# Step 2: The Provider Factory
class LLMFactory:
    @staticmethod
    def get_provider() -> BaseLLMService:
        """
        Instantiates the selected LLM provider based on the LLM_PROVIDER environment variable.
        """
        provider_type = os.getenv("LLM_PROVIDER", "gemini").lower()

        if provider_type == "gemini":
            api_key = os.getenv("GEMINI_API_KEY")
            model_name = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
            return GeminiService(api_key=api_key, model_name=model_name)

        elif provider_type == "vllm":
            base_url = os.getenv("VLLM_BASE_URL", "http://localhost:8000/v1")
            api_key = os.getenv("VLLM_API_KEY", "not-needed")
            model_name = os.getenv("VLLM_MODEL")
            # You can specify a local embedding model running in vLLM or fallback
            embedding_model = os.getenv("VLLM_EMBEDDING_MODEL") 
            return OpenAICompatibleService(
                base_url=base_url,
                api_key=api_key,
                model_name=model_name,
                embedding_model=embedding_model
            )

        elif provider_type == "ollama":
            base_url = os.getenv("OLLAMA_BASE_URL", "http://localhost:11434/v1")
            api_key = os.getenv("OLLAMA_API_KEY", "ollama")
            model_name = os.getenv("OLLAMA_MODEL", "llama3")
            embedding_model = os.getenv("OLLAMA_EMBEDDING_MODEL", "nomic-embed-text")
            return OpenAICompatibleService(
                base_url=base_url,
                api_key=api_key,
                model_name=model_name,
                embedding_model=embedding_model
            )

        elif provider_type == "openai":
            api_key = os.getenv("OPENAI_API_KEY")
            model_name = os.getenv("OPENAI_MODEL", "gpt-4o-mini")
            embedding_model = os.getenv("OPENAI_EMBEDDING_MODEL", "text-embedding-3-small")
            return OpenAICompatibleService(
                base_url=None, # Uses standard OpenAI URL
                api_key=api_key,
                model_name=model_name,
                embedding_model=embedding_model
            )

        else:
            raise ValueError(f"Unsupported LLM provider type: {provider_type}")


