"""L2 keywords for VoiceLabLibrary: ask the text agent, judge the answer, assert.

`Judge Answer` runs every judge for a case concurrently and records the verdicts; the
assertion keywords after it only read them, so a failing check never stops the others
from being recorded. With the judge off (no ANTHROPIC_API_KEY, or judge=off), the
judged checks log "skipped" and pass, and the deterministic ones still run.
"""

from __future__ import annotations

import asyncio
import math
from typing import Any

from robot.api import logger
from robot.api.deco import keyword

from lab.harness import l2


class L2Keywords:
    # Provided by VoiceLabLibrary: cfg, cases, judge_enabled, agent_model, _run, RESULTS_DIR

    def _l2_init(self) -> None:
        self._agent = None
        self._agent_cm = None
        self._judges = None
        self._ragas_llm = None
        self._ragas_emb = None
        self._turns: dict[str, Any] = {}
        self.l2_results: dict[str, dict[str, Any]] = {}

    def _case(self, key: str):
        """Golden case for a result key: a case id (L2), or "ID|variant|persona" (L3)."""
        return self.cases[key.split("|")[0]]

    # ------------------------------------------------------------ lifecycle

    @keyword
    def start_agent(self) -> None:
        """Start the text agent (MCP server over stdio + agent LLM) and the judges."""
        from lab.agent.text_agent import open_agent

        if self.agent_model:
            self.cfg.models["agent_llm"].name = self.agent_model
        self._agent_cm = open_agent(self.cfg, log_file=self.RESULTS_DIR / "mcp-server.log")
        self._agent = self._run(self._agent_cm.__aenter__())
        if hasattr(self._agent.llm, "ensure_model"):
            self._run(self._agent.llm.ensure_model(), timeout=3600)
        # Load the model now, so the first golden case isn't charged for it.
        self._run(self._agent.llm.chat([{"role": "user", "content": "Reply with OK."}]),
                  timeout=600)
        if self.judge_enabled:
            from lab import judge
            from lab.harness.judges import Judges

            self._judges = Judges(self.cfg)
            self._ragas_llm = judge.ragas_llm(self.cfg)
            self._ragas_emb = judge.ragas_embeddings(self.cfg)
        ref = self.cfg.models["agent_llm"]
        logger.info(f"agent {ref.provider}:{ref.name} at {self.cfg.endpoints.agent}, "
                    f"judge {'on (' + self._judge_name() + ')' if self.judge_enabled else 'off'}",
                    also_console=True)

    @keyword
    def finish_l2_run(self) -> str:
        """Write the JSON score file and stop the agent. Returns the file path."""
        try:
            ref = self.cfg.models["agent_llm"]
            extra = {
                "agent": {"provider": ref.provider, "model": ref.name,
                          "settings": ref.model_extra, "endpoint": self.cfg.endpoints.agent,
                          **self.cfg.agent.model_dump(mode="json"),
                          "prompt_sha": self._prompt_sha()},
                "aggregate": l2.aggregate(list(self.l2_results.values())),
                "cases": list(self.l2_results.values()),
            }
            path = self._write_report("L2", extra)
            agg = extra["aggregate"]
            logger.info(f"behavior {agg['behavior_accuracy']}, correct "
                        f"{agg['correctness_pass_rate']}, faithfulness {agg['faithfulness']}, "
                        f"relevancy {agg['answer_relevancy']}, speakable "
                        f"{agg['speakability_pass_rate']}, p95 "
                        f"{agg['latency_ms']['total_p95']} ms", also_console=True)
            return path
        finally:
            if self._agent_cm is not None:
                self._run(self._agent_cm.__aexit__(None, None, None))

    def _prompt_sha(self) -> str:
        import hashlib

        return hashlib.sha256(self.cfg.agent.prompt_text().encode()).hexdigest()[:12]

    # ---------------------------------------------------------------- agent

    @keyword
    def ask_agent(self, case_id: str) -> str:
        """Ask the golden question; record the turn, plus whether the plain question
        finds a gold source (to tell a bad agent query from a retrieval miss)."""
        case = self._case(case_id)
        turn = self._run(self._agent.ask(case.question), timeout=600)
        self._turns[case_id] = turn
        rec = l2.evaluate(case, turn)
        if case.gold_sources:
            res = self._run(self._agent.mcp.call_tool(
                "search_docs", {"query": case.question, "k": self.cfg.agent.search_k}))
            rec["canonical_gold_hit"] = l2.gold_hit(case, res.structured_content["hits"])
        self.l2_results[case_id] = rec
        for c in turn.tool_calls:
            logger.info(f"{c.name} {c.args} -> "
                        + (f"ERROR {c.error}" if c.error else "; ".join(
                            f"{h['repo']}:{h['path']} :: {h['heading']}" for h in c.hits)))
        t = turn.timing
        logger.info(f"ANSWER ({turn.stop}, {t['total_ms']:.0f} ms): {turn.answer}")
        if turn.spoken != turn.answer:
            logger.info(f"SPOKEN: {turn.spoken}")
        return turn.answer

    @keyword
    def judge_answer(self, case_id: str) -> None:
        """Run the judges for this case concurrently and record their verdicts. Never fails."""
        if not self.judge_enabled:
            logger.info("judges skipped (judge off)")
            return
        case, rec, turn = self._case(case_id), self.l2_results[case_id], self._turns[case_id]
        answer = turn.answer or "(no response)"
        spoken = turn.spoken or answer
        is_answer = case.expected_behavior == "answer"
        jobs: dict[str, Any] = {
            "policy": self._judges.policy(case.question, answer),
            "speakability": self._judges.speakability(case.question, spoken),
        }
        if is_answer:
            jobs["correctness"] = self._judges.correctness(
                case.question, answer, case.expected_answer, case.notes)
            if turn.answer:
                jobs["answer_relevancy"] = self._ragas("answer_relevancy", case, turn)
                if turn.contexts:
                    jobs["faithfulness"] = self._ragas("faithfulness", case, turn)

        async def gather() -> list[Any]:
            return await asyncio.gather(*jobs.values(), return_exceptions=True)

        for name, value in zip(jobs, self._run(gather(), timeout=600), strict=True):
            if isinstance(value, BaseException):
                logger.warn(f"{case_id} {name} judge failed: {value!r}")
                value = None
            rec[name] = value
            logger.info(f"{name}: {value}")
        if rec.get("policy"):
            rec["behavior_ok"] = rec["policy"]["verdict"] == case.expected_behavior

    async def _ragas(self, metric: str, case, turn) -> float | None:
        from ragas.metrics.collections import AnswerRelevancy, Faithfulness

        if metric == "faithfulness":
            res = await Faithfulness(llm=self._ragas_llm).ascore(
                user_input=case.question, response=turn.answer,
                retrieved_contexts=turn.contexts)
        else:
            # strictness=1: at temperature 0 the generated questions repeat anyway.
            res = await AnswerRelevancy(llm=self._ragas_llm, embeddings=self._ragas_emb,
                                        strictness=1).ascore(user_input=case.question,
                                                             response=turn.answer)
        value = float(res.value)
        return None if math.isnan(value) else round(value, 4)

    # --------------------------------------------------------------- checks

    def _judged(self, case_id: str, name: str) -> dict[str, Any] | None:
        verdict = self.l2_results[case_id].get(name)
        if verdict is None:
            logger.info(f"{name} skipped (no verdict)")
        return verdict

    @keyword
    def agent_should_have_searched(self, case_id: str) -> None:
        rec = self.l2_results[case_id]
        if not rec["searched"]:
            raise AssertionError("the agent answered without calling search_docs"
                                 + (f" (tool errors: {rec['tool_errors']})"
                                    if rec["tool_errors"] else ""))

    @keyword
    def agent_search_should_find_gold_source(self, case_id: str) -> None:
        """Fails only when the agent's own queries miss every gold source that the plain
        golden question finds. When both miss, that is retrieval's failure (see L1)."""
        rec = self.l2_results[case_id]
        if l2.query_regressed(rec):
            raise AssertionError(f"agent queries {rec['queries']} missed every gold source; "
                                 "the golden question itself finds one")
        if rec["agent_gold_hit"] is False:
            logger.warn(f"{case_id}: no gold source retrieved by the agent or the golden "
                        "question -- a retrieval miss, not an agent failure")

    def _behavior(self, case_id: str, expected: str) -> None:
        verdict = self._judged(case_id, "policy")
        if verdict and verdict["verdict"] != expected:
            raise AssertionError(f"expected {expected}, the agent's response reads as "
                                 f"{verdict['verdict']}: {verdict['reason']}")

    @keyword
    def agent_should_answer(self, case_id: str) -> None:
        self._behavior(case_id, "answer")

    @keyword
    def agent_should_decline(self, case_id: str) -> None:
        self._behavior(case_id, "decline")

    @keyword
    def agent_should_ask_for_clarification(self, case_id: str) -> None:
        self._behavior(case_id, "clarify")

    @keyword
    def answer_should_contain_expected_terms(self, case_id: str) -> None:
        terms = self.l2_results[case_id]["terms"]
        case = self._case(case_id)
        problems = []
        if terms["missing"]:
            problems.append(f"missing {terms['missing']}")
        if not terms["any_ok"]:
            problems.append(f"none of {case.must_include_any}")
        if terms["forbidden"]:
            problems.append(f"contains forbidden {terms['forbidden']}")
        if problems:
            raise AssertionError("; ".join(problems))

    @keyword
    def answer_should_be_correct(self, case_id: str) -> None:
        verdict = self._judged(case_id, "correctness")
        if verdict and verdict["verdict"] != "pass":
            raise AssertionError(f"incorrect (score {verdict['score']}): {verdict['reason']}")

    @keyword
    def answer_should_be_faithful(self, case_id: str, threshold: float | None = None) -> None:
        """Ragas faithfulness: share of the answer's claims supported by what was retrieved."""
        value = self.l2_results[case_id].get("faithfulness")
        threshold = float(threshold or self.cfg.thresholds.get("faithfulness", 0.8))
        if value is None:
            logger.info("faithfulness skipped (no answer, no context, or judge off)")
        elif value < threshold:
            raise AssertionError(f"faithfulness {value} < {threshold}: the answer makes "
                                 "claims the retrieved context does not support")

    @keyword
    def answer_should_be_speakable(self, case_id: str) -> None:
        verdict = self._judged(case_id, "speakability")
        if verdict and verdict["verdict"] != "pass":
            raise AssertionError(f"not speakable: {verdict['reason']}")

    # ------------------------------------------------------------ run level

    @keyword
    def l2_aggregates_should_meet_thresholds(self) -> None:
        agg = l2.aggregate(list(self.l2_results.values()))
        t = self.cfg.thresholds
        checks = []
        if self.judge_enabled:
            checks = [("behavior_accuracy", agg["behavior_accuracy"], "behavior_accuracy"),
                      ("correctness_pass_rate", agg["correctness_pass_rate"], "correctness"),
                      ("faithfulness", agg["faithfulness"], "faithfulness"),
                      ("answer_relevancy", agg["answer_relevancy"], "answer_relevancy"),
                      ("speakability_pass_rate", agg["speakability_pass_rate"],
                       "speakability")]
        failures = []
        for name, value, key in checks:
            threshold = t.get(key)
            logger.info(f"{name}: {value} (threshold {threshold})")
            if threshold is not None and (value is None or value < threshold):
                failures.append(f"{name} {value} < {threshold}")
        logger.info(f"search rate {agg['search_rate_answer_cases']}, agent gold hit "
                    f"{agg['agent_gold_hit_rate']} vs golden question "
                    f"{agg['canonical_gold_hit_rate']}, latency {agg['latency_ms']}")
        if failures:
            raise AssertionError("; ".join(failures))
