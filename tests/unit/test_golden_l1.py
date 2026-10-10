import pytest
from pydantic import ValidationError

from lab.harness import golden, l1
from lab.harness.golden import GoldenCase


def hit(repo, path, heading="H", origin=None, score=0.8):
    return {"repo": repo, "path": path, "heading": heading, "score": score,
            "origin": origin or ("fork" if repo.startswith("billmallard/") else "upstream")}


def case(**kw):
    base = {"id": "T-1", "category": "factual", "question": "q?", "expected_answer": "a",
            "expected_behavior": "answer",
            "gold_sources": [{"repo": "billmallard/pyEfis", "path": "README.rst",
                              "heading": "Hardware"}]}
    return GoldenCase.model_validate({**base, **kw})


def test_repo_golden_set_is_valid():
    cases = golden.load()
    assert len(cases) >= 30
    for c in cases:
        if c.expected_behavior == "answer":
            assert c.gold_sources, f"{c.id}: answerable case without gold sources"
        assert not c.review, f"{c.id}: unresolved review question"
        assert not set(map(str, c.gold_sources)) & set(map(str, c.trap_sources)), c.id


def test_schema_rejects_misspelled_fields():
    with pytest.raises(ValidationError):
        case(must_includ=["x"])


def test_heading_is_a_substring_match():
    ref = case().gold_sources[0]
    assert ref.matches(hit("billmallard/pyEfis", "README.rst", "pyEfis > Hardware > Minimum"))
    assert not ref.matches(hit("billmallard/pyEfis", "README.rst", "pyEfis > Testing"))
    assert not ref.matches(hit("makerplane/pyEfis", "README.rst", "pyEfis > Hardware"))


def test_evaluate_ranks_traps_and_precision():
    c = case(trap_sources=[{"repo": "billmallard/pyEfis", "path": "docs/perf.md"}])
    hits = [hit("billmallard/pyEfis", "docs/perf.md"),
            hit("billmallard/pyEfis", "README.rst", "pyEfis > Hardware"),
            hit("makerplane/pyEfis", "README.rst")]
    r = l1.evaluate(c, hits)
    assert (r["gold_rank"], r["trap_rank"], r["trap_above_gold"]) == (2, 1, True)
    assert r["precision_at_k"] == pytest.approx(1 / 3)
    assert r["reciprocal_rank"] == 0.5


def test_trap_with_no_gold_counts_as_above_gold():
    c = case(trap_sources=[{"repo": "billmallard/pyEfis", "path": "INSTALLING.md"}])
    r = l1.evaluate(c, [hit("billmallard/pyEfis", "INSTALLING.md")])
    assert r["hit"] is False and r["trap_above_gold"] is True


def test_version_cases_check_top_hit_origin():
    c = case(category="version")
    assert l1.evaluate(c, [hit("makerplane/pyEfis", "README.rst")])["origin_ok"] is False
    assert l1.evaluate(c, [hit("billmallard/pyEfis", "x.md")])["origin_ok"] is True
    assert "origin_ok" not in l1.evaluate(case(), [hit("makerplane/pyEfis", "x.md")])


def test_aggregate():
    c = case()
    results = [l1.evaluate(c, [hit("billmallard/pyEfis", "README.rst", "Hardware")]),
               l1.evaluate(c, [hit("makerplane/pyEfis", "x.md")])]
    results[0]["ragas_context_precision"] = 1.0
    agg = l1.aggregate(results, [50.0, 60.0, 70.0])
    assert agg["hit_rate"] == 0.5 and agg["mrr"] == 0.5
    assert agg["misses"] == ["T-1"]
    assert agg["ragas_context_precision"] == 1.0  # mean over cases that were scored
    assert agg["latency_ms"]["max"] == 70.0
