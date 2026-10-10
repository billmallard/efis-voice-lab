"""Failure attribution: one primary root-cause tag per failed case, from the turn data.

Rules run in order; the first that matches is the primary tag, and every match is kept
in `tags` so the report can show compound failures. A tag names the layer to fix.

  TELEPHONY                passes over the voice path (L3) but fails over the phone (L4)
  STT_MISHEARD             the STT judge says the transcript doesn't mean the question;
                           with no judge verdict, WER over stt_wer_max. WER alone doesn't
                           decide when the judge ran: it counts "PI-FEs" for "pie efis" as
                           two errors, though the meaning survives.
  TURN_TAKING              the caller's one question became several turns, or barge-in /
                           silence was mishandled
  POLICY                   answered / declined / asked for clarification wrongly
  RETRIEVAL_MISS           no gold source in the agent's results, and the plain question
                           doesn't find one either: an index or corpus gap
  AGENT_QUERY              the agent's own search missed gold sources the plain question finds
  RETRIEVAL_WRONG_VERSION  version question; the agent's top hit comes from the wrong origin
  CORPUS_TRAP              a known-stale source (golden trap_sources) was retrieved and
                           the answer is wrong: fix the document, not the agent
  GENERATION_UNFAITHFUL    the answer makes claims its retrieved context doesn't support
  GENERATION_INCORRECT     good context, a faithful answer, still wrong
  TERMS                    only the required/forbidden term checks failed
  GENERATION_UNSPEAKABLE   right answer, wrong for the ear
  LATENCY                  content passed; time to first audio is over budget

Deviation from the plan's order: POLICY runs before the retrieval and generation rules.
When the agent answers a question it should have declined, a retrieval tag on that answer
would point at the wrong layer. TURN_TAKING moves up for the same reason: an answer to
half a question says nothing about retrieval. AGENT_QUERY, CORPUS_TRAP,
GENERATION_INCORRECT and TERMS are additions that M4's L2 results called for.
"""

from __future__ import annotations

from typing import Any

from lab.harness.golden import GoldenCase

CONTENT_TAGS = ("TELEPHONY", "STT_MISHEARD", "TURN_TAKING", "POLICY", "RETRIEVAL_MISS",
                "AGENT_QUERY", "RETRIEVAL_WRONG_VERSION", "CORPUS_TRAP",
                "GENERATION_UNFAITHFUL", "GENERATION_INCORRECT", "TERMS",
                "GENERATION_UNSPEAKABLE")
TAGS = (*CONTENT_TAGS, "LATENCY")


def _verdict(rec: dict[str, Any], key: str) -> str | None:
    v = rec.get(key)
    return v.get("verdict") if isinstance(v, dict) else None


def tags_for(case: GoldenCase, rec: dict[str, Any], thresholds: dict[str, float],
             latency_budget_ms: float | None = None) -> list[str]:
    """Every tag that applies, in rule order. An empty list means the case passed."""
    out: list[str] = []
    answer_case = case.expected_behavior == "answer"
    if rec.get("l4_failed") and rec.get("l3_passed"):
        out.append("TELEPHONY")

    stt = rec.get("stt")
    if stt is not None:
        wer_max = thresholds.get("stt_wer_max", 0.25)
        verdict = _verdict(rec, "stt")
        if verdict == "different" or (verdict is None and stt.get("wer") is not None
                                      and stt["wer"] > wer_max):
            out.append("STT_MISHEARD")
    if rec.get("turn_taking_failed"):
        out.append("TURN_TAKING")
    if rec.get("behavior_ok") is False:
        out.append("POLICY")

    if answer_case and case.gold_sources and rec.get("searched"):
        if rec.get("agent_gold_hit") is False:
            out.append("AGENT_QUERY" if rec.get("canonical_gold_hit") else "RETRIEVAL_MISS")
    elif answer_case and case.gold_sources and not rec.get("searched"):
        out.append("AGENT_QUERY")                   # answered from memory, no search at all

    retrieved = rec.get("retrieved") or []
    if (case.category == "version" and retrieved
            and retrieved[0].get("origin") != case.expected_origin):
        out.append("RETRIEVAL_WRONG_VERSION")

    incorrect = _verdict(rec, "correctness") == "fail"
    trap_hit = any(ref.matches({**h, "heading": h.get("heading", "")})
                   for ref in case.trap_sources for h in retrieved)
    if incorrect and trap_hit:
        out.append("CORPUS_TRAP")
    faith = rec.get("faithfulness")
    if faith is not None and faith < thresholds.get("faithfulness", 0.8):
        out.append("GENERATION_UNFAITHFUL")
    if incorrect and not trap_hit and "GENERATION_UNFAITHFUL" not in out:
        out.append("GENERATION_INCORRECT")
    terms = rec.get("terms") or {}
    if terms and not terms.get("ok", True) and not out:
        out.append("TERMS")
    if _verdict(rec, "speakability") == "fail":
        out.append("GENERATION_UNSPEAKABLE")

    ttfa = (rec.get("timing") or {}).get("time_to_first_audio_ms")
    if latency_budget_ms is not None and ttfa is not None and ttfa > latency_budget_ms:
        out.append("LATENCY")
    return out


def attribute(case: GoldenCase, rec: dict[str, Any], thresholds: dict[str, float],
              latency_budget_ms: float | None = None) -> dict[str, Any]:
    tags = tags_for(case, rec, thresholds, latency_budget_ms)
    content = [t for t in tags if t in CONTENT_TAGS]
    return {"primary": tags[0] if tags else None, "tags": tags,
            "content_ok": not content, "passed": not tags}


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Counts by primary tag, plus pass rates with and without the latency budget."""
    counts = {t: 0 for t in TAGS}
    for r in records:
        primary = (r.get("attribution") or {}).get("primary")
        if primary:
            counts[primary] += 1
    n = len(records)
    att = [r.get("attribution") or {} for r in records]
    return {
        "cases": n,
        "pass_rate": round(sum(a.get("passed", False) for a in att) / n, 4) if n else None,
        "content_pass_rate": round(sum(a.get("content_ok", False) for a in att) / n, 4)
        if n else None,
        "by_primary_tag": {t: c for t, c in counts.items() if c},
    }
