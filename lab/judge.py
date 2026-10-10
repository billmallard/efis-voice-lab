"""The judge LLM (config `models.judge_llm`), shared by Ragas metrics and custom judges.

Anthropic SDK 1.x removed `temperature` / `top_p` / `top_k` from `messages.create()`,
but instructor (which Ragas uses for its LLM calls) still passes `temperature`. The
judge model (Haiku 4.5) still honours sampling settings, and a low temperature keeps
judge verdicts repeatable, so the shim forwards them in `extra_body` rather than
dropping them. Remove it once instructor stops sending them.
"""

from __future__ import annotations

import functools
from typing import Any

from lab.config import LabConfig

_SAMPLING = ("temperature", "top_p", "top_k")

# Judge token usage for the current process; run reports price it with config.
USAGE = {"requests": 0, "input_tokens": 0, "output_tokens": 0}


def record_usage(usage: Any) -> None:
    if usage is not None:
        USAGE["requests"] += 1
        USAGE["input_tokens"] += usage.input_tokens or 0
        USAGE["output_tokens"] += usage.output_tokens or 0


def usage_cost(cfg: LabConfig) -> dict[str, float | int]:
    extra = cfg.models["judge_llm"].model_extra or {}
    cost = (USAGE["input_tokens"] * float(extra.get("usd_per_mtok_in", 0))
            + USAGE["output_tokens"] * float(extra.get("usd_per_mtok_out", 0))) / 1e6
    return {**USAGE, "usd": round(cost, 4)}


def anthropic_client() -> Any:
    from anthropic import AsyncAnthropic  # optional dependency: the `eval` extra

    client = AsyncAnthropic()  # ANTHROPIC_API_KEY, loaded from .env by lab.config
    create = client.messages.create

    @functools.wraps(create)
    async def create_with_sampling(*args: Any, **kwargs: Any) -> Any:
        sampling = {k: kwargs.pop(k) for k in _SAMPLING if k in kwargs}
        # The API takes temperature or top_p, not both; instructor sends both.
        if "temperature" in sampling:
            sampling = {"temperature": sampling["temperature"]}
        if sampling:
            kwargs["extra_body"] = {**kwargs.get("extra_body", {}), **sampling}
        response = await create(*args, **kwargs)
        record_usage(getattr(response, "usage", None))
        return response

    client.messages.create = create_with_sampling
    return client


def ragas_llm(cfg: LabConfig) -> Any:
    from ragas.llms import llm_factory

    ref = cfg.models["judge_llm"]
    if ref.provider != "anthropic":
        raise ValueError(f"unsupported judge provider: {ref.provider!r}")
    extra = ref.model_extra or {}
    return llm_factory(ref.name, provider="anthropic", client=anthropic_client(),
                       max_tokens=int(extra.get("max_tokens", 2048)))


def ragas_embeddings(cfg: LabConfig) -> Any:
    """The lab's own embedding model (config `models.embedding`) as a Ragas embedding, for
    AnswerRelevancy. Its texts are questions, so they get the query prefix."""
    import asyncio

    from ragas.embeddings.base import BaseRagasEmbedding

    from lab import embeddings

    emb = embeddings.from_config(cfg)

    class LabEmbedding(BaseRagasEmbedding):
        def embed_text(self, text: str, **kwargs: Any) -> list[float]:
            return emb.embed_query(text)

        async def aembed_text(self, text: str, **kwargs: Any) -> list[float]:
            return await asyncio.to_thread(emb.embed_query, text)

    return LabEmbedding()


def judge_name(cfg: LabConfig) -> str:
    ref = cfg.models["judge_llm"]
    return f"{ref.provider}:{ref.name}"
