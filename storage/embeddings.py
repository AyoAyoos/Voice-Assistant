"""Local-only embeddings through the user's existing Ollama service.

No hosted embedding provider is used. If Ollama is unavailable the callers
fall back to exact keyword search, so the assistant still works.
"""

from __future__ import annotations

import os
from typing import Sequence

import httpx
from loguru import logger


class OllamaEmbedder:
    """Small async client for Ollama's ``/api/embed`` endpoint."""

    def __init__(self) -> None:
        self.base_url = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434").rstrip("/")
        self.model = os.getenv("OLLAMA_EMBED_MODEL", "nomic-embed-text")
        self.expected_dim = self._positive_int("EMBEDDING_DIM", 768)
        self.timeout = self._positive_float("OLLAMA_TIMEOUT_SECONDS", 10.0)
        self._client: httpx.AsyncClient | None = None

    @staticmethod
    def _positive_int(name: str, default: int) -> int:
        try:
            value = int(os.getenv(name, str(default)))
            return value if value > 0 else default
        except ValueError:
            logger.error(f"Invalid {name}; using default {default}")
            return default

    @staticmethod
    def _positive_float(name: str, default: float) -> float:
        try:
            value = float(os.getenv(name, str(default)))
            return value if value > 0 else default
        except ValueError:
            logger.error(f"Invalid {name}; using default {default}")
            return default

    async def _get_client(self) -> httpx.AsyncClient:
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=self.timeout)
        return self._client

    async def embed_texts(self, texts: Sequence[str]) -> list[list[float]] | None:
        cleaned = [text.strip() for text in texts if text and text.strip()]
        if not cleaned:
            return []

        try:
            client = await self._get_client()
            response = await client.post(
                f"{self.base_url}/api/embed",
                json={"model": self.model, "input": cleaned},
            )
            response.raise_for_status()
            payload = response.json()
            vectors = payload.get("embeddings")
            if not isinstance(vectors, list) or len(vectors) != len(cleaned):
                raise ValueError(
                    f"Expected {len(cleaned)} embeddings, got "
                    f"{len(vectors) if isinstance(vectors, list) else type(vectors).__name__}"
                )

            result: list[list[float]] = []
            for vector in vectors:
                if not isinstance(vector, list) or len(vector) != self.expected_dim:
                    raise ValueError(
                        f"Expected {self.expected_dim}-dim embedding, got "
                        f"{len(vector) if isinstance(vector, list) else type(vector).__name__}"
                    )
                result.append([float(value) for value in vector])
            return result
        except Exception as exc:
            logger.error(
                f"Ollama embedding failed ({self.model} @ {self.base_url}): {exc}"
            )
            return None

    async def embed_text(self, text: str) -> list[float] | None:
        vectors = await self.embed_texts([text])
        return vectors[0] if vectors else None

    async def close(self) -> None:
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None


embedder = OllamaEmbedder()
