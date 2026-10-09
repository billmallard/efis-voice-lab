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


class LabConfig(BaseModel):
    endpoints: Endpoints = Endpoints()
    models: dict[str, ModelRef]
    retrieval: Retrieval = Retrieval()
    thresholds: dict[str, float] = {}


def load(path: str | Path | None = None) -> LabConfig:
    path = Path(path or os.environ.get("LAB_CONFIG", DEFAULT_PATH))
    raw: dict[str, Any] = yaml.safe_load(path.read_text(encoding="utf-8"))
    cfg = LabConfig.model_validate(raw)
    for key in ("ollama", "qdrant"):
        env = os.environ.get(f"LAB_{key.upper()}_URL")
        if env:
            setattr(cfg.endpoints, key, env)
    return cfg
