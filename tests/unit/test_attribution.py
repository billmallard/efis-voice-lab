from lab.harness import attribution
from lab.harness.golden import GoldenCase

T = {"faithfulness": 0.8, "stt_wer_max": 0.25}


def case(**kw):
    base = {"id": "T-1", "category": "factual", "question": "q?", "expected_answer": "a",
            "expected_behavior": "answer",
            "gold_sources": [{"repo": "billmallard/pyEfis", "path": "README.rst"}],
            "trap_sources": [{"repo": "billmallard/pyEfis", "path": "INSTALLING.md"}]}
    return GoldenCase.model_validate({**base, **kw})


def rec(**kw):
    base = {"searched": True, "agent_gold_hit": True, "canonical_gold_hit": True,
            "behavior_ok": True, "faithfulness": 1.0, "terms": {"ok": True},
            "correctness": {"verdict": "pass"}, "speakability": {"verdict": "pass"},
            "retrieved": [{"repo": "billmallard/pyEfis", "path": "README.rst",
                           "origin": "fork", "heading": "H"}],
            "timing": {"time_to_first_audio_ms": 900.0}}
    return {**base, **kw}


def primary(c, r, budget=1500):
    return attribution.attribute(c, r, T, budget)["primary"]


def test_clean_case_passes():
    out = attribution.attribute(case(), rec(), T, 1500)
    assert out == {"primary": None, "tags": [], "content_ok": True, "passed": True}


def test_stt_comes_first():
    r = rec(stt={"verdict": "different", "wer": 0.1}, correctness={"verdict": "fail"})
    assert primary(case(), r) == "STT_MISHEARD"
    assert primary(case(), rec(stt={"wer": 0.6})) == "STT_MISHEARD"          # no judge
    # The judge's "equivalent" outranks a high WER (a respelled product name).
    assert primary(case(), rec(stt={"verdict": "equivalent", "wer": 0.6})) is None


def test_policy_precedes_retrieval():
    r = rec(behavior_ok=False, agent_gold_hit=False, canonical_gold_hit=False)
    assert attribution.tags_for(case(), r, T)[:2] == ["POLICY", "RETRIEVAL_MISS"]


def test_query_versus_index_miss():
    assert primary(case(), rec(agent_gold_hit=False, canonical_gold_hit=True)) == "AGENT_QUERY"
    assert primary(case(), rec(agent_gold_hit=False, canonical_gold_hit=False)) == \
        "RETRIEVAL_MISS"
    assert primary(case(), rec(searched=False)) == "AGENT_QUERY"


def test_trap_blames_the_corpus():
    r = rec(correctness={"verdict": "fail"},
            retrieved=[{"repo": "billmallard/pyEfis", "path": "INSTALLING.md",
                        "origin": "fork", "heading": "Install"}])
    assert primary(case(), r) == "CORPUS_TRAP"
    assert primary(case(), rec(correctness={"verdict": "fail"})) == "GENERATION_INCORRECT"
    assert primary(case(), rec(faithfulness=0.3, correctness={"verdict": "fail"})) == \
        "GENERATION_UNFAITHFUL"


def test_wrong_version():
    c = case(category="version")
    r = rec(retrieved=[{"repo": "makerplane/pyEfis", "path": "README.rst",
                        "origin": "upstream", "heading": "H"}])
    assert "RETRIEVAL_WRONG_VERSION" in attribution.tags_for(c, r, T)


def test_latency_only_when_content_passes_or_as_secondary():
    slow = rec(timing={"time_to_first_audio_ms": 12000.0})
    out = attribution.attribute(case(), slow, T, 1500)
    assert out["primary"] == "LATENCY" and out["content_ok"] and not out["passed"]
    out = attribution.attribute(case(), {**slow, "speakability": {"verdict": "fail"}}, T, 1500)
    assert out["tags"] == ["GENERATION_UNSPEAKABLE", "LATENCY"]


def test_terms_only_when_nothing_else_explains_it():
    assert primary(case(), rec(terms={"ok": False})) == "TERMS"
    assert primary(case(), rec(terms={"ok": False}, correctness={"verdict": "fail"})) == \
        "GENERATION_INCORRECT"


def test_summary_counts_primary_tags():
    rs = [{"attribution": attribution.attribute(case(), rec(), T, 1500)},
          {"attribution": attribution.attribute(case(), rec(behavior_ok=False), T, 1500)}]
    s = attribution.summarize(rs)
    assert s["by_primary_tag"] == {"POLICY": 1} and s["pass_rate"] == 0.5


def test_wer():
    from lab.harness.l3 import wer

    assert wer("Do I need a GPU?", "do i need a gpu") == 0.0
    assert wer("does pie efis need a graphics card", "Does PI-FEs need a graphics card?") == \
        round(2 / 7, 4)
    assert wer("one two", "1 2") == 0.0
    assert wer("", "") == 0.0
