"""L1 (retrieval) scoring: pure functions over a golden case and a ranked hit list.

Deterministic measures need no LLM:
  gold_rank         1-based rank of the first hit matching any gold source (None = miss)
  trap_rank         same for trap sources (stale / misleading documents)
  trap_above_gold   a trap outranks every gold hit: the retriever is steering toward
                    a confident wrong answer, and the corpus should take the blame
  precision_at_k    share of the k hits that match some gold source
  reciprocal_rank   1 / gold_rank (0 on a miss); averaged, it is MRR
  origin_ok         version questions: the top hit comes from the expected origin

Ragas context precision / recall (LLM-judged) are added by the caller when enabled.
"""

from __future__ import annotations

import statistics
from typing import Any

from lab.harness.golden import GoldenCase, SourceRef


def rank_of(refs: list[SourceRef], hits: list[dict[str, Any]]) -> int | None:
    for i, hit in enumerate(hits, 1):
        if any(ref.matches(hit) for ref in refs):
            return i
    return None


def evaluate(case: GoldenCase, hits: list[dict[str, Any]]) -> dict[str, Any]:
    gold_rank = rank_of(case.gold_sources, hits)
    trap_rank = rank_of(case.trap_sources, hits) if case.trap_sources else None
    gold_hits = sum(any(r.matches(h) for r in case.gold_sources) for h in hits)
    out: dict[str, Any] = {
        "id": case.id,
        "category": case.category,
        "question": case.question,
        "k": len(hits),
        "gold_rank": gold_rank,
        "hit": gold_rank is not None,
        "trap_rank": trap_rank,
        "trap_above_gold": trap_rank is not None and (gold_rank is None or trap_rank < gold_rank),
        "precision_at_k": gold_hits / len(hits) if hits else 0.0,
        "reciprocal_rank": 1 / gold_rank if gold_rank else 0.0,
        "top_hit": {k: hits[0][k] for k in ("repo", "origin", "path", "heading", "score")}
        if hits else None,
    }
    if case.category == "version" and hits:
        out["expected_origin"] = case.expected_origin
        out["origin_ok"] = hits[0]["origin"] == case.expected_origin
    return out


def percentile(values: list[float], pct: int) -> float:
    if len(values) < 2:
        return values[0] if values else 0.0
    return statistics.quantiles(values, n=100, method="inclusive")[pct - 1]


def aggregate(results: list[dict[str, Any]], latencies_ms: list[float]) -> dict[str, Any]:
    n = len(results)

    def mean(key: str) -> float | None:
        vals = [r[key] for r in results if r.get(key) is not None]
        return round(statistics.fmean(vals), 4) if vals else None

    version = [r for r in results if "origin_ok" in r]
    return {
        "cases": n,
        "hit_rate": round(sum(r["hit"] for r in results) / n, 4) if n else None,
        "mrr": mean("reciprocal_rank"),
        "precision_at_k": mean("precision_at_k"),
        "traps_above_gold": [r["id"] for r in results if r["trap_above_gold"]],
        "misses": [r["id"] for r in results if not r["hit"]],
        "origin_accuracy": round(sum(r["origin_ok"] for r in version) / len(version), 4)
        if version else None,
        "ragas_context_precision": mean("ragas_context_precision"),
        "ragas_context_recall": mean("ragas_context_recall"),
        "latency_ms": {
            "n": len(latencies_ms),
            "p50": round(percentile(latencies_ms, 50), 1),
            "p95": round(percentile(latencies_ms, 95), 1),
            "max": round(max(latencies_ms), 1) if latencies_ms else 0.0,
        },
    }
