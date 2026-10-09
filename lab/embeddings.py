"""Embedding providers, selected by config `models.embedding.provider`.

Some models expect task prefixes on their input (nomic-embed-text wants
`search_document: ` / `search_query: `). Those live in config next to the model
name, so swapping models never means editing code.
"""

from __future__ import annotations

from typing import Protocol

import httpx

from lab.config import LabConfig


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbedder:
    def __init__(self, base_url: str, name: str, timeout: float = 300.0):
        self.name = name
        self._client = httpx.Client(base_url=base_url, timeout=timeout)
        self._ready = False

    def ensure_model(self) -> None:
        """Pull the model if the Ollama server does not have it yet."""
        if self._ready:
            return
        if self._client.post("/api/show", json={"model": self.name}).status_code == 404:
            r = self._client.post(
                "/api/pull", json={"model": self.name, "stream": False}, timeout=1800
            )
            r.raise_for_status()
        self._ready = True

    def embed(self, texts: list[str]) -> list[list[float]]:
        self.ensure_model()
        # /api/embed (batch) superseded /api/embeddings (single prompt).
        r = self._client.post("/api/embed", json={"model": self.name, "input": texts})
        r.raise_for_status()
        return r.json()["embeddings"]


class PrefixedEmbedder:
    """Applies the model's document/query prefixes and batches large inputs."""

    def __init__(self, inner: Embedder, document_prefix: str = "", query_prefix: str = "",
                 batch_size: int = 32):
        self.inner = inner
        self.name = inner.name
        self.document_prefix = document_prefix
        self.query_prefix = query_prefix
        self.batch_size = batch_size

    def embed_documents(self, texts: list[str]) -> list[list[float]]:
        out: list[list[float]] = []
        for i in range(0, len(texts), self.batch_size):
            batch = texts[i : i + self.batch_size]
            out.extend(self.inner.embed([self.document_prefix + t for t in batch]))
        return out

    def embed_query(self, text: str) -> list[float]:
        return self.inner.embed([self.query_prefix + text])[0]


def from_config(cfg: LabConfig) -> PrefixedEmbedder:
    ref = cfg.models["embedding"]
    if ref.provider == "ollama":
        inner: Embedder = OllamaEmbedder(cfg.endpoints.ollama, ref.name)
    else:
        raise ValueError(f"unsupported embedding provider: {ref.provider!r}")
    extra = ref.model_extra or {}
    return PrefixedEmbedder(
        inner,
        document_prefix=extra.get("document_prefix", ""),
        query_prefix=extra.get("query_prefix", ""),
        batch_size=int(extra.get("batch_size", 32)),
    )
