"""Embedding providers, selected by config `models.embedding.provider`."""

from __future__ import annotations

from typing import Protocol

import httpx

from lab.config import LabConfig


class Embedder(Protocol):
    name: str

    def embed(self, texts: list[str]) -> list[list[float]]: ...


class OllamaEmbedder:
    def __init__(self, base_url: str, name: str, timeout: float = 120.0):
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


def from_config(cfg: LabConfig) -> Embedder:
    ref = cfg.models["embedding"]
    if ref.provider == "ollama":
        return OllamaEmbedder(cfg.endpoints.ollama, ref.name)
    raise ValueError(f"unsupported embedding provider: {ref.provider!r}")
