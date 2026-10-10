"""L3 (voice) scoring: pure functions.

  wer                  word error rate of the STT transcript against the words the caller
                       actually said (the spoken variant), after normalization
  aggregate            run-level voice metrics, on top of the L2 aggregate of the same records
"""

from __future__ import annotations

import re
import statistics
from typing import Any

from lab.harness import attribution, l2
from lab.harness.l1 import percentile

_NUMBERS = {"zero": "0", "one": "1", "two": "2", "three": "3", "four": "4", "five": "5",
            "six": "6", "seven": "7", "eight": "8", "nine": "9", "ten": "10"}


def normalize_words(text: str) -> list[str]:
    text = text.lower().replace("-", " ")
    words = re.findall(r"[a-z0-9']+", text)
    return [_NUMBERS.get(w, w) for w in words]


def wer(reference: str, hypothesis: str) -> float:
    """Word-level Levenshtein distance over the reference length."""
    ref, hyp = normalize_words(reference), normalize_words(hypothesis)
    if not ref:
        return 0.0 if not hyp else 1.0
    prev = list(range(len(hyp) + 1))
    for i, r in enumerate(ref, 1):
        cur = [i] + [0] * len(hyp)
        for j, h in enumerate(hyp, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (r != h))
        prev = cur
    return round(prev[-1] / len(ref), 4)


def _vals(records: list[dict[str, Any]], key: str) -> list[float]:
    return [r["timing"][key] for r in records
            if r.get("timing") and r["timing"].get(key) is not None]


def aggregate(records: list[dict[str, Any]], budget_ms: float | None) -> dict[str, Any]:
    turns = [r for r in records if r.get("kind", "turn") == "turn"]
    ttfa = _vals(turns, "time_to_first_audio_ms")
    stt = [r for r in turns if r.get("stt")]

    def pct(key: str, p: int) -> float | None:
        v = _vals(turns, key)
        return round(percentile(v, p), 1) if v else None

    out = {
        "turns": len(turns),
        "stt_wer_mean": round(statistics.fmean([r["stt"]["wer"] for r in stt]), 4)
        if stt else None,
        "stt_equivalent_rate": round(sum(r["stt"].get("verdict") == "equivalent"
                                         for r in stt) / len(stt), 4)
        if stt and any("verdict" in r["stt"] for r in stt) else None,
        "turn_splits": [r["id"] for r in turns if r.get("turn_split")],
        "latency_ms": {
            "time_to_first_audio_p50": pct("time_to_first_audio_ms", 50),
            "time_to_first_audio_p95": pct("time_to_first_audio_ms", 95),
            "stt_p50": pct("stt_ms", 50),
            "agent_p50": pct("agent_ms", 50),
            "tts_first_audio_p50": pct("tts_first_audio_ms", 50),
            "budget": budget_ms,
            "within_budget_rate": round(sum(t <= budget_ms for t in ttfa) / len(ttfa), 4)
            if ttfa and budget_ms else None,
        },
        "by_persona": {},
        "behavior": [{k: r.get(k) for k in ("id", "scenario", "ok", "detail")}
                     for r in records if r.get("kind") == "behavior"],
        "attribution": attribution.summarize(turns),
        "agent": l2.aggregate(turns) if turns else None,
    }
    for persona in sorted({r.get("persona") for r in turns if r.get("persona")}):
        rs = [r for r in turns if r.get("persona") == persona]
        out["by_persona"][persona] = {
            "turns": len(rs),
            "content_pass_rate": round(sum((r.get("attribution") or {}).get("content_ok",
                                                                             False)
                                           for r in rs) / len(rs), 4),
            "stt_wer_mean": round(statistics.fmean([r["stt"]["wer"] for r in rs
                                                    if r.get("stt")]), 4)
            if any(r.get("stt") for r in rs) else None,
        }
    return out
