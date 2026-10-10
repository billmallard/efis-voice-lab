"""Custom LLM judges: policy, correctness, speakability, STT equivalence.

Each judge sends its rubric (lab/harness/rubrics/<name>.md) as the system prompt and
gets a structured verdict back through `messages.parse`, using one fixed shape:
verdict (a per-judge label), score (0 to 1), reason. Ragas metrics go through Ragas'
own adapter instead (lab/judge.py).

Prompt caching does not apply: Haiku 4.5's minimum cacheable prefix is 4,096 tokens,
and every rubric is far shorter.
"""

from __future__ import annotations

import functools
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, Field

from lab import judge
from lab.config import LabConfig

RUBRICS = Path(__file__).parent / "rubrics"


class _Verdict(BaseModel):
    score: float = Field(ge=0, le=1)
    reason: str


class PolicyVerdict(_Verdict):
    verdict: Literal["answer", "decline", "clarify"]


class PassFail(_Verdict):
    verdict: Literal["pass", "fail"]


class SttVerdict(_Verdict):
    verdict: Literal["equivalent", "different"]


class Contradiction(BaseModel):
    claim: str = Field(description="what the answer says")
    conflicts_with: str = Field(description="the sentence of the reference or notes it "
                                            "contradicts, quoted exactly")


class CorrectnessFindings(BaseModel):
    """The correctness judge lists findings; `Judges.correctness` derives the verdict, so
    an omission can never fail a case on its own (calibration, 2026-10-10)."""
    answers_question: bool
    main_claim_correct: bool
    contradictions: list[Contradiction]
    omissions: list[str]
    score: float = Field(ge=0, le=1)
    reason: str


@functools.cache
def rubric(name: str) -> str:
    return (RUBRICS / f"{name}.md").read_text(encoding="utf-8").strip()


def _tagged(**fields: str | None) -> str:
    return "\n\n".join(f"<{k}>\n{v.strip()}\n</{k}>" for k, v in fields.items() if v)


class Judges:
    def __init__(self, cfg: LabConfig, client: Any | None = None):
        ref = cfg.models["judge_llm"]
        if ref.provider != "anthropic":
            raise ValueError(f"unsupported judge provider: {ref.provider!r}")
        self.model = ref.name
        if client is None:
            from anthropic import AsyncAnthropic

            client = AsyncAnthropic()
        self.client = client

    async def _judge(self, name: str, schema: type[BaseModel], content: str) -> dict[str, Any]:
        resp = await self.client.messages.parse(
            model=self.model, max_tokens=1024, system=rubric(name),
            messages=[{"role": "user", "content": content}], output_format=schema,
            # SDK 1.x dropped the temperature kwarg; the API still takes it.
            extra_body={"temperature": 0})
        judge.record_usage(resp.usage)
        out = resp.parsed_output.model_dump()
        out["score"] = round(out["score"], 3)
        return out

    async def policy(self, question: str, answer: str) -> dict[str, Any]:
        return await self._judge("policy", PolicyVerdict,
                                 _tagged(caller_question=question, assistant_response=answer))

    async def correctness(self, question: str, answer: str, reference: str,
                          notes: str | None = None) -> dict[str, Any]:
        found = await self._judge("correctness", CorrectnessFindings, _tagged(
            question=question, reference_answer=reference, notes=notes,
            assistant_answer=answer))
        # A contradiction counts only if it quotes text that is really in the reference or
        # notes; the rest are kept for the record but don't fail the case.
        source = " ".join(f"{reference} {notes or ''}".split()).lower()

        def quoted(c: dict[str, str]) -> bool:
            return " ".join(c["conflicts_with"].split()).lower().strip(" .\"'") in source

        found["unverified_contradictions"] = [c for c in found["contradictions"]
                                              if not quoted(c)]
        found["contradictions"] = [c for c in found["contradictions"] if quoted(c)]
        ok = (found["answers_question"] and found["main_claim_correct"]
              and not found["contradictions"])
        return {"verdict": "pass" if ok else "fail", **found}

    async def speakability(self, question: str, answer: str) -> dict[str, Any]:
        return await self._judge("speakability", PassFail,
                                 _tagged(caller_question=question, assistant_response=answer))

    async def stt_equivalence(self, intended: str, transcript: str) -> dict[str, Any]:
        return await self._judge("stt_equivalence", SttVerdict,
                                 _tagged(intended_question=intended, transcript=transcript))
