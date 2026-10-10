"""L2 (agent, text in / text out) scoring: pure functions over a golden case, an agent
turn, and the judge verdicts the harness adds.

Deterministic measures:
  searched          the agent called search_docs (required for `answer` cases)
  agent_gold_hit    a gold source is among the chunks the agent's own queries retrieved
  canonical_gold_hit  the same for the golden question sent straight to search (the L1
                    view). A miss on the agent's query where the canonical question
                    hits is the agent's fault (a bad query), not retrieval's.
  terms             must_include / must_include_any / must_not_include, case-insensitive

Judged (lab/harness/judges.py, Ragas): policy (answer / decline / clarify),
correctness against the reference answer, speakability, Ragas faithfulness and answer
relevancy.
"""

from __future__ import annotations

import statistics
from typing import Any

from lab.agent.text_agent import AgentTurn
from lab.harness.golden import GoldenCase
from lab.harness.l1 import percentile


def term_checks(case: GoldenCase, answer: str) -> dict[str, Any]:
    text = answer.lower()
    missing = [t for t in case.must_include if t.lower() not in text]
    any_ok = (any(t.lower() in text for t in case.must_include_any)
              if case.must_include_any else True)
    forbidden = [t for t in case.must_not_include if t.lower() in text]
    return {"missing": missing, "any_ok": any_ok, "forbidden": forbidden,
            "ok": not missing and any_ok and not forbidden}


def gold_hit(case: GoldenCase, hits: list[dict[str, Any]]) -> bool | None:
    if not case.gold_sources:
        return None
    return any(ref.matches(h) for ref in case.gold_sources for h in hits)


def evaluate(case: GoldenCase, turn: AgentTurn) -> dict[str, Any]:
    return {
        "id": case.id,
        "category": case.category,
        "expected_behavior": case.expected_behavior,
        "question": case.question,
        "answer": turn.answer,
        "spoken": turn.spoken or turn.answer,
        "stop": turn.stop,
        "rounds": turn.rounds,
        "searched": turn.searched,
        "queries": [c.args.get("query") for c in turn.tool_calls if c.name == "search_docs"],
        "tool_errors": [c.error for c in turn.tool_calls if c.error],
        "retrieved": turn.retrieved,
        "agent_gold_hit": gold_hit(case, turn.retrieved),
        # What the caller would hear is what's checked.
        "terms": term_checks(case, turn.spoken or turn.answer),
        "timing": turn.timing,
        "usage": turn.usage,
    }


def query_regressed(rec: dict[str, Any]) -> bool:
    """The agent's own query missed every gold source that the plain question finds."""
    return (rec.get("searched") and rec.get("agent_gold_hit") is False
            and rec.get("canonical_gold_hit") is True)


def _rate(values: list[bool]) -> float | None:
    return round(sum(values) / len(values), 4) if values else None


def _mean(values: list[float | None]) -> float | None:
    vals = [v for v in values if v is not None]
    return round(statistics.fmean(vals), 4) if vals else None


def aggregate(results: list[dict[str, Any]]) -> dict[str, Any]:
    answer = [r for r in results if r["expected_behavior"] == "answer"]
    judged = [r for r in results if r.get("policy")]
    total_ms = [r["timing"]["total_ms"] for r in results]
    first_ms = [r["timing"]["first_llm_ms"] for r in results]

    def by_behavior(expected: str) -> float | None:
        rows = [r for r in judged if r["expected_behavior"] == expected]
        return _rate([r["behavior_ok"] for r in rows])

    correct = [r for r in answer if r.get("correctness")]
    speak = [r for r in results if r.get("speakability")]
    return {
        "cases": len(results),
        "behavior_accuracy": _rate([r["behavior_ok"] for r in judged]),
        "behavior_accuracy_by_expected": {b: by_behavior(b)
                                          for b in ("answer", "decline", "clarify")},
        "behavior_misses": [f"{r['id']}: {r['policy']['verdict']} (expected "
                            f"{r['expected_behavior']})" for r in judged if not r["behavior_ok"]],
        "correctness_pass_rate": _rate([r["correctness"]["verdict"] == "pass"
                                        for r in correct]),
        "correctness_mean_score": _mean([r["correctness"]["score"] for r in correct]),
        "incorrect": [r["id"] for r in correct if r["correctness"]["verdict"] != "pass"],
        "faithfulness": _mean([r.get("faithfulness") for r in answer]),
        "answer_relevancy": _mean([r.get("answer_relevancy") for r in answer]),
        "speakability_pass_rate": _rate([r["speakability"]["verdict"] == "pass"
                                         for r in speak]),
        "unspeakable": [r["id"] for r in speak if r["speakability"]["verdict"] != "pass"],
        "term_pass_rate": _rate([r["terms"]["ok"] for r in results]),
        "search_rate_answer_cases": _rate([r["searched"] for r in answer]),
        "agent_gold_hit_rate": _rate([r["agent_gold_hit"] for r in answer
                                      if r["agent_gold_hit"] is not None]),
        "canonical_gold_hit_rate": _rate([r["canonical_gold_hit"] for r in answer
                                          if r.get("canonical_gold_hit") is not None]),
        "query_regressions": [r["id"] for r in results if query_regressed(r)],
        "latency_ms": {"total_p50": round(percentile(total_ms, 50), 1),
                       "total_p95": round(percentile(total_ms, 95), 1),
                       "first_llm_p50": round(percentile(first_ms, 50), 1),
                       "first_llm_p95": round(percentile(first_ms, 95), 1)},
        "agent_tokens": {k: sum(r["usage"][k] for r in results)
                         for k in ("input_tokens", "output_tokens")},
    }
