"""Text agent loop, provider message conversion, L2 scoring and judge plumbing, all with
fakes: no LLM, MCP server or network."""

import asyncio
from types import SimpleNamespace

import pytest

from lab import config, judge
from lab.agent import llm
from lab.agent.llm import ChatResult, ToolCallRequest
from lab.agent.text_agent import AgentTurn, TextAgent
from lab.harness import judges, l2
from lab.harness.golden import GoldenCase

SEARCH_SCHEMA = {
    "type": "object",
    "properties": {"query": {"type": "string"}, "k": {"type": "integer"},
                   "origin": {"type": "string"}, "repo": {"type": "string"}},
    "required": ["query", "k"],
}


def hit(i, repo="billmallard/pyEfis", path="README.rst", heading="pyEfis > Hardware"):
    return {"id": f"h{i}", "score": 0.9 - i / 10, "repo": repo, "path": path,
            "heading": heading, "origin": "fork", "text": f"chunk {i}"}


class FakeMcp:
    def __init__(self, hits):
        self.hits = hits
        self.calls = []

    async def list_tools(self):
        return [SimpleNamespace(name="search_docs", description="search",
                                input_schema=SEARCH_SCHEMA),
                SimpleNamespace(name="get_source", description="doc", input_schema={})]

    async def call_tool(self, name, args, raise_on_error=True):
        self.calls.append((name, args))
        return SimpleNamespace(is_error=False, content=[],
                               structured_content={"hits": self.hits})


class ScriptedLLM:
    name = "scripted"

    def __init__(self, replies):
        self.replies = list(replies)
        self.seen_tools = []

    async def chat(self, messages, tools=None):
        self.seen_tools.append(tools)
        return self.replies.pop(0)

    async def aclose(self):
        pass


def search(query):
    return ChatResult(text="", tool_calls=[ToolCallRequest(id="c1", name="search_docs",
                                                           args={"query": query,
                                                                 "origin": "null"})],
                      input_tokens=10, output_tokens=5, ms=1.0)


def ask(replies, hits=None, **agent_cfg):
    cfg = config.load()
    cfg.agent = cfg.agent.model_copy(update=agent_cfg)
    mcp, chat = FakeMcp(hits if hits is not None else [hit(1), hit(2)]), ScriptedLLM(replies)
    turn = asyncio.run(TextAgent(cfg, mcp, chat).ask("Do I need a GPU?"))
    return turn, mcp, chat


def test_tools_hide_harness_args_and_unlisted_tools():
    _, mcp, chat = ask([search("gpu"), ChatResult(text="Only for synthetic vision.")])
    offered = chat.seen_tools[0]
    assert [t["name"] for t in offered] == ["search_docs"]
    assert set(offered[0]["input_schema"]["properties"]) == {"query"}
    assert offered[0]["input_schema"]["required"] == ["query"]
    # The model's mangled filter is dropped; k comes from config.
    assert mcp.calls == [("search_docs", {"query": "gpu", "k": 5})]


def test_turn_records_tool_calls_context_and_usage():
    turn, _, _ = ask([search("gpu"), ChatResult(text="Only for synthetic vision.",
                                                input_tokens=20, output_tokens=7, ms=2.0)])
    assert turn.answer == "Only for synthetic vision."
    assert (turn.stop, turn.rounds, turn.searched) == ("answered", 2, True)
    assert turn.contexts == ["chunk 1", "chunk 2"]
    assert turn.retrieved[0]["path"] == "README.rst"
    assert turn.usage == {"input_tokens": 30, "output_tokens": 12}
    assert "text" not in turn.tool_calls[0].hits[0]


def test_tool_budget_forces_an_answer_without_tools():
    turn, _, chat = ask([search("a"), ChatResult(text="")], max_tool_rounds=1)
    assert chat.seen_tools[1] is None
    assert turn.stop == "tool_budget"


def test_unknown_tool_is_recorded_not_raised():
    bad = ChatResult(text="", tool_calls=[ToolCallRequest(id="x", name="get_weather",
                                                          args={})])
    turn, mcp, _ = ask([bad, ChatResult(text="I can only help with pyEfis.")])
    assert turn.tool_calls[0].error == "unknown tool"
    assert not turn.searched and mcp.calls == []


def test_anthropic_conversion_groups_tool_results():
    msgs = [{"role": "system", "content": "S"}, {"role": "user", "content": "Q"},
            {"role": "assistant", "content": "",
             "tool_calls": [ToolCallRequest(id="a", name="search_docs", args={"query": "x"}),
                            ToolCallRequest(id="b", name="search_docs", args={"query": "y"})]},
            {"role": "tool", "tool_call_id": "a", "name": "search_docs", "content": "r1"},
            {"role": "tool", "tool_call_id": "b", "name": "search_docs", "content": "r2"}]
    system, out = llm.AnthropicChat._messages(msgs)
    assert system == "S"
    assert [m["role"] for m in out] == ["user", "assistant", "user"]
    assert [b["tool_use_id"] for b in out[2]["content"]] == ["a", "b"]
    assert [b["type"] for b in out[1]["content"]] == ["tool_use", "tool_use"]


def test_ollama_conversion():
    msgs = [{"role": "assistant", "content": "",
             "tool_calls": [ToolCallRequest(id="a", name="search_docs", args={"query": "x"})]},
            {"role": "tool", "tool_call_id": "a", "name": "search_docs", "content": "r"}]
    out = llm.OllamaChat._messages(msgs)
    assert out[0]["tool_calls"] == [{"function": {"name": "search_docs",
                                                  "arguments": {"query": "x"}}}]
    assert out[1] == {"role": "tool", "content": "r", "tool_name": "search_docs"}


def golden_case(**kw):
    base = {"id": "T-1", "category": "factual", "question": "q?", "expected_answer": "a",
            "expected_behavior": "answer",
            "gold_sources": [{"repo": "billmallard/pyEfis", "path": "README.rst",
                              "heading": "Hardware"}]}
    return GoldenCase.model_validate({**base, **kw})


def test_term_checks_are_case_insensitive():
    c = golden_case(must_include=["Synthetic Vision"], must_include_any=["CPU", "processor"],
                    must_not_include=["bracket"])
    assert l2.term_checks(c, "Only for synthetic vision; the cpu draws the rest.")["ok"]
    r = l2.term_checks(c, "Press the left bracket key.")
    assert r["missing"] == ["Synthetic Vision"] and not r["any_ok"]
    assert r["forbidden"] == ["bracket"] and not r["ok"]


def make_turn(answer="ok", retrieved=()):
    return AgentTurn(question="q?", answer=answer, model="m", stop="answered", rounds=2,
                     tool_calls=[], contexts=[], retrieved=list(retrieved),
                     timing={"total_ms": 100.0, "llm_ms": 90.0, "tool_ms": 5.0,
                             "first_llm_ms": 40.0},
                     usage={"input_tokens": 10, "output_tokens": 3})


def test_query_regression_needs_a_canonical_hit():
    c = golden_case()
    rec = l2.evaluate(c, make_turn(retrieved=[hit(1, path="INSTALLING.md")]))
    rec["searched"] = True
    assert rec["agent_gold_hit"] is False
    rec["canonical_gold_hit"] = False
    assert not l2.query_regressed(rec)        # retrieval's miss, not the agent's
    rec["canonical_gold_hit"] = True
    assert l2.query_regressed(rec)


def test_aggregate_scores_judged_cases():
    a = l2.evaluate(golden_case(id="A"), make_turn(retrieved=[hit(1)]))
    a.update(policy={"verdict": "answer"}, behavior_ok=True,
             correctness={"verdict": "pass", "score": 1.0},
             speakability={"verdict": "fail", "score": 0.2}, faithfulness=0.5)
    d = l2.evaluate(golden_case(id="D", category="out_of_scope", expected_behavior="decline",
                                gold_sources=[]), make_turn())
    d.update(policy={"verdict": "answer"}, behavior_ok=False,
             speakability={"verdict": "pass", "score": 1.0})
    agg = l2.aggregate([a, d])
    assert agg["behavior_accuracy"] == 0.5
    assert agg["behavior_accuracy_by_expected"]["decline"] == 0.0
    assert agg["behavior_misses"] == ["D: answer (expected decline)"]
    assert agg["correctness_pass_rate"] == 1.0
    assert agg["speakability_pass_rate"] == 0.5 and agg["unspeakable"] == ["A"]
    assert agg["faithfulness"] == 0.5 and agg["agent_gold_hit_rate"] == 1.0


class FakeAnthropic:
    def __init__(self, parsed):
        self.kwargs = None
        self.parsed = parsed
        self.messages = self

    async def parse(self, **kwargs):
        self.kwargs = kwargs
        return SimpleNamespace(parsed_output=kwargs["output_format"](**self.parsed),
                               usage=SimpleNamespace(input_tokens=100, output_tokens=20))


def test_judge_sends_rubric_and_counts_usage():
    fake = FakeAnthropic({"verdict": "decline", "score": 0.91234, "reason": "refused"})
    before = judge.USAGE["input_tokens"]
    out = asyncio.run(judges.Judges(config.load(), client=fake).policy("q?", "I can't."))
    assert out == {"verdict": "decline", "score": 0.912, "reason": "refused"}
    assert fake.kwargs["system"] == judges.rubric("policy")
    assert fake.kwargs["model"] == config.load().models["judge_llm"].name
    assert "<assistant_response>\nI can't.\n</assistant_response>" in \
        fake.kwargs["messages"][0]["content"]
    assert judge.USAGE["input_tokens"] - before == 100


def test_judge_verdict_labels_are_constrained():
    with pytest.raises(ValueError):
        judges.PolicyVerdict(verdict="maybe", score=0.5, reason="x")
    with pytest.raises(ValueError):
        judges.PassFail(verdict="pass", score=1.5, reason="x")


def test_agent_config_and_env_override(monkeypatch):
    monkeypatch.setenv("LAB_AGENT_LLM_URL", "http://gpu-host:11435")
    monkeypatch.setenv("LAB_AGENT_MODEL", "qwen3:8b")
    cfg = config.load()
    assert cfg.endpoints.agent == "http://gpu-host:11435"
    assert cfg.models["agent_llm"].name == "qwen3:8b"
    assert "search_docs" in cfg.agent.tools and cfg.agent.prompt_text()


def test_context_precision_matches_ragas_formula():
    from lab.harness.calibration import context_precision

    assert context_precision([True, False, False, False, False]) == 1.0
    assert context_precision([False, True, False, False, True]) == round((1 / 2 + 2 / 5) / 2, 4)
    assert context_precision([False] * 5) == 0.0


def test_calibration_sample_keeps_every_non_answer_case():
    from lab.harness.calibration import sample

    cases = [{"id": f"A-{i}", "expected_behavior": "answer"} for i in range(30)]
    cases += [{"id": "D-1", "expected_behavior": "decline"},
              {"id": "C-1", "expected_behavior": "clarify"}]
    picked = sample(cases, 10)
    assert len(picked) == 10 and {"D-1", "C-1"} <= {c["id"] for c in picked}
    assert picked == sample(cases, 10)


def test_cohen_kappa():
    from lab.harness.calibration import cohen_kappa

    assert cohen_kappa([("pass", "pass"), ("fail", "fail")]) == 1.0
    assert cohen_kappa([("pass", "fail"), ("fail", "pass")]) == -1.0
    assert cohen_kappa([]) is None
