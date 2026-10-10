"""Load config/lab.yaml. Endpoints can be overridden from the environment."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any

import yaml
from pydantic import BaseModel

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_PATH = ROOT / "config" / "lab.yaml"


class ModelRef(BaseModel):
    provider: str | None = None
    name: str | None = None
    model_config = {"extra": "allow"}  # e.g. stt.size, tts.voice


class Endpoints(BaseModel):
    ollama: str = "http://localhost:11434"
    qdrant: str = "http://localhost:6333"


class Retrieval(BaseModel):
    top_k: int = 5
    p95_budget_ms: int = 300


class Index(BaseModel):
    collection: str = "efis_docs"


class Ingest(BaseModel):
    cache_dir: Path = Path("data/sources")
    max_chunk_chars: int = 1500
    min_chunk_chars: int = 40

    def cache_path(self) -> Path:
        return self.cache_dir if self.cache_dir.is_absolute() else ROOT / self.cache_dir


class LabConfig(BaseModel):
    endpoints: Endpoints = Endpoints()
    models: dict[str, ModelRef]
    retrieval: Retrieval = Retrieval()
    index: Index = Index()
    ingest: Ingest = Ingest()
    thresholds: dict[str, float] = {}


def load_dotenv(path: Path = ROOT / ".env") -> None:
    """Minimal KEY=VALUE reader for the gitignored .env; real environment variables win."""
    if not path.is_file():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def load(path: str | Path | None = None) -> LabConfig:
    load_dotenv()
    path = Path(path or os.environ.get("LAB_CONFIG", DEFAULT_PATH))
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg = LabConfig.model_validate(raw)
    for key in ("ollama", "qdrant"):
        env = os.environ.get(f"LAB_{key.upper()}_URL")
        if env:
            setattr(cfg.endpoints, key, env)
    return cfg
