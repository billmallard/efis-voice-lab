from lab import config


def test_default_config_loads():
    cfg = config.load()
    assert cfg.models["embedding"].provider == "ollama"
    assert cfg.retrieval.top_k > 0
    assert 0 < cfg.thresholds["faithfulness"] <= 1


def test_endpoint_env_override(monkeypatch):
    monkeypatch.setenv("LAB_QDRANT_URL", "http://qdrant.example:6333")
    assert config.load().endpoints.qdrant == "http://qdrant.example:6333"
