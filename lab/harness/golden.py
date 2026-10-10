"""The golden set (golden/questions.yaml), loaded with a strict schema: a misspelled
field is an error, not a silently ignored key."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Literal

import yaml
from pydantic import BaseModel, ConfigDict

from lab.config import ROOT

DEFAULT_PATH = ROOT / "golden" / "questions.yaml"

Category = Literal["factual", "conditional", "procedural", "version", "speakability",
                   "conflict", "clarify", "out_of_scope"]


class SourceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")
    repo: str
    path: str
    heading: str | None = None

    def matches(self, hit: dict[str, Any]) -> bool:
        return (hit["repo"] == self.repo and hit["path"] == self.path
                and (self.heading is None or self.heading in hit["heading"]))

    def __str__(self) -> str:
        return f"{self.repo}:{self.path}" + (f" [{self.heading}]" if self.heading else "")


class GoldenCase(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str
    category: Category
    question: str
    spoken_variants: list[str] = []
    expected_answer: str
    gold_sources: list[SourceRef] = []
    trap_sources: list[SourceRef] = []
    must_include: list[str] = []
    must_include_any: list[str] = []
    must_not_include: list[str] = []
    expected_behavior: Literal["answer", "decline", "clarify"]
    notes: str | None = None
    review: str | None = None

    @property
    def scored_in_l1(self) -> bool:
        """Retrieval is only scored where there is something to retrieve."""
        return bool(self.gold_sources)

    @property
    def expected_origin(self) -> str:
        """fork | upstream, from the first (primary) gold source's repo owner."""
        return "fork" if self.gold_sources[0].repo.startswith("billmallard/") else "upstream"


def load(path: str | Path | None = None) -> list[GoldenCase]:
    raw = yaml.safe_load(Path(path or DEFAULT_PATH).read_text(encoding="utf-8"))
    cases = [GoldenCase.model_validate(item) for item in raw]
    dupes = {c.id for c in cases if sum(d.id == c.id for d in cases) > 1}
    if dupes:
        raise ValueError(f"duplicate golden ids: {sorted(dupes)}")
    return cases
