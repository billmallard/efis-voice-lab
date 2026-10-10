"""Judge calibration: a person grades a sample of cases blind, and the harness reports how
often the judges agree before anyone trusts their trends.

  lab calibrate export   writes golden/calibration/<run>.yaml from an L2 run: the
                         question, reference answer, notes and the agent's answer for a
                         stratified sample of cases, with empty grade fields. Judge
                         verdicts are left out so the grader can't anchor on them. It also
                         lists the retrieved chunks for the L1 cases where M3 found the
                         deterministic score and Ragas context precision disagreeing.
  lab calibrate score    compares the filled-in grades with the run's judge verdicts:
                         agreement rate and Cohen's kappa per judge, plus each
                         disagreement with the judge's reason.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from pathlib import Path
from typing import Any

import yaml

from lab.config import ROOT, LabConfig
from lab.harness import golden

CAL_DIR = ROOT / "golden" / "calibration"
# M3: deterministic retrieval score and Ragas context precision disagreed on these.
L1_CASES = ("DATA-001", "DEV-003", "FGW-003")
JUDGED = {"behavior": "policy", "correct": "correctness", "speakable": "speakability"}


class _Folded(str):
    pass


yaml.SafeDumper.add_representer(
    _Folded, lambda d, s: d.represent_scalar("tag:yaml.org,2002:str", s, style=">"))


def _fold(text: str | None) -> Any:
    if not text:
        return text
    text = " ".join(text.split())
    return _Folded(text) if len(text) > 70 else text


def sample(cases: list[dict[str, Any]], n: int) -> list[dict[str, Any]]:
    """Every decline and clarify case, then answer cases in a fixed pseudo-random order
    (by id hash), up to n. Never chosen by verdict, which would bias the agreement."""
    other = [c for c in cases if c["expected_behavior"] != "answer"]
    answer = sorted((c for c in cases if c["expected_behavior"] == "answer"),
                    key=lambda c: hashlib.sha256(c["id"].encode()).hexdigest())
    picked = {c["id"] for c in other + answer[: max(0, n - len(other))]}
    return [c for c in cases if c["id"] in picked]


def context_precision(relevant: list[bool]) -> float:
    """Ragas' context precision formula on human labels: mean precision@k over the ranks
    holding a relevant chunk."""
    hits, total = 0, 0.0
    for k, rel in enumerate(relevant, 1):
        if rel:
            hits += 1
            total += hits / k
    return round(total / hits, 4) if hits else 0.0


def export(cfg: LabConfig, run_path: Path, n: int = 20, l1_baseline: Path | None = None,
           out: Path | None = None) -> Path:
    from lab.retrieval.core import Retriever

    run = json.loads(run_path.read_text(encoding="utf-8"))
    cases = {c.id: c for c in golden.load()}
    rows = []
    for r in sample(run["cases"], n):
        g = cases[r["id"]]
        row = {"id": r["id"], "expected_behavior": g.expected_behavior,
               "question": g.question, "reference": _fold(g.expected_answer)}
        if g.notes:
            row["notes"] = _fold(g.notes)
        row["answer"] = _fold(r["answer"]) or "(no response)"
        grade = {"behavior": None}
        if g.expected_behavior == "answer":
            grade["correct"] = None
        grade.update(speakable=None, comment=None)
        row["grade"] = grade
        rows.append(row)

    l1_rows = []
    retriever = Retriever(cfg)
    for cid in L1_CASES:
        g = cases[cid]
        hits = retriever.search(g.question, cfg.retrieval.top_k).hits
        l1_rows.append({"id": cid, "question": g.question,
                        "reference": _fold(g.expected_answer),
                        "chunks": [{"rank": i, "source": f"{h.repo}:{h.path} :: {h.heading}",
                                    "text": _fold(h.text[:600]), "relevant": None}
                                   for i, h in enumerate(hits, 1)]})

    def rel(path: Path | None) -> str | None:
        if path is None:
            return None
        path = path.resolve()
        return (path.relative_to(ROOT) if path.is_relative_to(ROOT) else path).as_posix()

    doc = {
        "run": rel(run_path),
        "l1_baseline": rel(l1_baseline),
        "agent": run.get("agent", {}).get("model"),
        "judge": run.get("judge"),
        "grader": None,
        "instructions": _fold(
            "Grade each case yourself, without looking at the judge verdicts in the run "
            "file. behavior: answer, decline or clarify (what the response does, right or "
            "wrong). correct: pass or fail against the reference answer and notes; omitting "
            "secondary detail is fine, contradicting the reference is not. speakable: pass "
            "or fail as if heard on the phone (no markup, paths, code or symbols read "
            "aloud; short unless it's a how-to). For the L1 cases, mark each chunk relevant "
            "true or false: would it help answer the question as the reference does?"),
        "cases": rows,
        "l1_context_precision": l1_rows,
    }
    CAL_DIR.mkdir(parents=True, exist_ok=True)
    out = out or CAL_DIR / f"L2-{run['started_at'][:10]}.yaml"
    out.write_text(yaml.safe_dump(doc, sort_keys=False, allow_unicode=True, width=92),
                   encoding="utf-8")
    return out


def cohen_kappa(pairs: list[tuple[str, str]]) -> float | None:
    n = len(pairs)
    if not n:
        return None
    po = sum(a == b for a, b in pairs) / n
    ca, cb = Counter(a for a, _ in pairs), Counter(b for _, b in pairs)
    pe = sum(ca[k] * cb[k] for k in ca) / n / n
    return round((po - pe) / (1 - pe), 3) if pe < 1 else (1.0 if po == 1 else 0.0)


def score(sheet_path: Path) -> dict[str, Any]:
    sheet = yaml.safe_load(sheet_path.read_text(encoding="utf-8"))
    run = json.loads((ROOT / sheet["run"]).read_text(encoding="utf-8"))
    verdicts = {c["id"]: c for c in run["cases"]}
    report: dict[str, Any] = {"run": sheet["run"], "grader": sheet.get("grader"),
                              "judge": run.get("judge"), "judges": {}}
    for field, judge_key in JUDGED.items():
        pairs, disagreements = [], []
        for row in sheet["cases"]:
            human = (row.get("grade") or {}).get(field)
            v = verdicts.get(row["id"], {}).get(judge_key)
            if human is None or not v:
                continue
            human = str(human).strip().lower()
            pairs.append((human, v["verdict"]))
            if human != v["verdict"]:
                disagreements.append({"id": row["id"], "human": human,
                                      "judge": v["verdict"], "judge_reason": v["reason"],
                                      "comment": row["grade"].get("comment")})
        agree = sum(a == b for a, b in pairs)
        report["judges"][judge_key] = {
            "graded": len(pairs), "agree": agree,
            "agreement": round(agree / len(pairs), 3) if pairs else None,
            "kappa": cohen_kappa(pairs), "disagreements": disagreements}

    l1 = {}
    if sheet.get("l1_baseline"):
        base = json.loads((ROOT / sheet["l1_baseline"]).read_text(encoding="utf-8"))
        ragas = {c["id"]: c.get("ragas_context_precision") for c in base["cases"]}
        for row in sheet.get("l1_context_precision") or []:
            labels = [c.get("relevant") for c in row["chunks"]]
            if any(lab is None for lab in labels):
                continue
            l1[row["id"]] = {"human": context_precision([bool(x) for x in labels]),
                             "ragas": ragas.get(row["id"])}
    report["l1_context_precision"] = l1
    return report
