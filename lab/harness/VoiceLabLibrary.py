# ruff: noqa: N999  -- Robot convention: module named after its library class
"""Robot Framework keyword library for the EFIS Voice Lab harness.

Import it in a suite and it generates one test per golden case (it is also a
listener), so `robot tests/L1_retrieval` or `robot tests/L2_agent` runs the whole golden
set without the cases being duplicated in .robot files. Hand-written tests in the suite
(latency, aggregates) run after the generated ones.

L1: retrieval goes through the real interfaces: the REST API (`lab serve-api`, a
separate process) for every query, and the MCP server over stdio (`lab serve-mcp`) to
check both return the same results.

L2: the text agent (lab/agent/text_agent.py) answers each question through the same MCP
server. Its keywords live in l2_keywords.py.
"""

from __future__ import annotations

import asyncio
import json
import os
import socket
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import httpx
from robot.api import logger
from robot.api.deco import keyword, library

from lab import config
from lab.config import ROOT
from lab.harness import golden, l1
from lab.harness.l2_keywords import L2Keywords

RESULTS_DIR = ROOT / "results"


class _GoldenTestGenerator:
    """Listener v3: add one test per scored golden case to the importing suite."""

    ROBOT_LISTENER_API_VERSION = 3

    def __init__(self, lib: VoiceLabLibrary):
        self.lib = lib
        self.done: set[str] = set()

    def start_suite(self, data, result) -> None:
        if data.longname in self.done:
            return
        self.done.add(data.longname)
        generate = {"L1": self._l1_tests, "L2": self._l2_tests}[self.lib.layer]
        generated = generate(data)
        # Generated cases first, so suite-level checks (latency, aggregates) see them all.
        handwritten = [t for t in data.tests if t not in generated]
        data.tests = generated + handwritten

    def _l1_tests(self, data) -> list:
        generated = []
        for case in self.lib.cases.values():
            if not case.scored_in_l1 or not self.lib.selected(case):
                continue
            test = data.tests.create(
                name=f"{case.id} {case.question}",
                doc=case.notes or "",
                tags=[case.id, case.category, "golden", "robot:continue-on-failure",
                      *sorted({f"repo:{g.repo}" for g in case.gold_sources}),
                      *(["trap"] if case.trap_sources else [])],
            )
            test.body.create_keyword(name="Search Docs", args=[case.question])
            test.body.create_keyword(name="MCP Results Should Match REST")
            test.body.create_keyword(name="Score Retrieval With Ragas", args=[case.id])
            test.body.create_keyword(name="Retrieval Should Include Gold Source", args=[case.id])
            if case.trap_sources:
                test.body.create_keyword(name="Trap Source Should Not Outrank Gold",
                                         args=[case.id])
            if case.category == "version":
                test.body.create_keyword(name="Top Hit Should Come From Expected Origin",
                                         args=[case.id])
            generated.append(test)
        return generated

    def _l2_tests(self, data) -> list:
        generated = []
        for case in self.lib.cases.values():
            if not self.lib.selected(case):
                continue
            test = data.tests.create(
                name=f"{case.id} {case.question}",
                doc=case.notes or "",
                tags=[case.id, case.category, case.expected_behavior, "golden",
                      "robot:continue-on-failure",
                      *(["trap"] if case.trap_sources else [])],
            )
            kw = test.body.create_keyword
            kw(name="Ask Agent", args=[case.id])
            kw(name="Judge Answer", args=[case.id])
            if case.expected_behavior == "answer":
                kw(name="Agent Should Have Searched", args=[case.id])
                if case.gold_sources:
                    kw(name="Agent Search Should Find Gold Source", args=[case.id])
                kw(name="Agent Should Answer", args=[case.id])
                kw(name="Answer Should Contain Expected Terms", args=[case.id])
                kw(name="Answer Should Be Correct", args=[case.id])
                kw(name="Answer Should Be Faithful", args=[case.id])
            elif case.expected_behavior == "decline":
                kw(name="Agent Should Decline", args=[case.id])
                kw(name="Answer Should Contain Expected Terms", args=[case.id])
            else:
                kw(name="Agent Should Ask For Clarification", args=[case.id])
            kw(name="Answer Should Be Speakable", args=[case.id])
            generated.append(test)
        return generated


class _McpSession:
    """An MCP stdio client living on a background event loop (Robot is synchronous)."""

    def __init__(self, loop: asyncio.AbstractEventLoop):
        from fastmcp import Client
        from fastmcp.client.transports import StdioTransport

        self.loop = loop
        # Robot replaces sys.stderr with an object that has no fileno; the server's
        # stderr needs a real file.
        log = RESULTS_DIR / "mcp-server.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        self.client = Client(StdioTransport(sys.executable, ["-m", "lab.cli", "serve-mcp"],
                                            cwd=str(ROOT), log_file=log))
        self.run(self.client.__aenter__())

    def run(self, coro, timeout: float = 120):
        return asyncio.run_coroutine_threadsafe(coro, self.loop).result(timeout)

    def search(self, query: str, k: int) -> dict[str, Any]:
        res = self.run(self.client.call_tool("search_docs", {"query": query, "k": k}))
        return res.structured_content

    def close(self) -> None:
        self.run(self.client.__aexit__(None, None, None))


@library(scope="SUITE", listener=None)
class VoiceLabLibrary(L2Keywords):
    RESULTS_DIR = RESULTS_DIR

    def __init__(self, layer: str = "L1", ragas: str = "auto", repo: str = "", ids: str = "",
                 judge: str = "", agent_model: str = ""):
        """`repo` / `ids` (comma-separated) narrow the generated cases. Robot's --include
        cannot: tag filtering happens before the listener adds the tests.

        `judge` is the L2 name for the `ragas` switch: auto = on when ANTHROPIC_API_KEY
        is set; on | off to force. `agent_model` overrides models.agent_llm.name."""
        self.layer = layer
        self.repo_filter = repo.strip()
        self.id_filter = {i.strip() for i in ids.split(",") if i.strip()}
        self.cfg = config.load()
        self.cases = {c.id: c for c in golden.load()}
        mode = judge or ragas
        self.ragas_enabled = (mode == "on"
                              or (mode == "auto" and bool(os.environ.get("ANTHROPIC_API_KEY"))))
        self.judge_enabled = self.ragas_enabled
        self.agent_model = agent_model.strip()
        self.k = self.cfg.retrieval.top_k
        self.ROBOT_LIBRARY_LISTENER = _GoldenTestGenerator(self)
        self._api: subprocess.Popen | None = None
        self._http: httpx.Client | None = None
        self._mcp: _McpSession | None = None
        self._loop = asyncio.new_event_loop()
        threading.Thread(target=self._loop.run_forever, daemon=True).start()
        self._ragas_llm = None
        self._last: dict[str, Any] | None = None
        self.results: dict[str, dict[str, Any]] = {}
        self.latencies_ms: list[float] = []
        self.started_at = datetime.now(UTC)
        self._l2_init()

    def _run(self, coro, timeout: float = 300):
        """Run a coroutine on the library's background event loop (Robot is synchronous)."""
        return asyncio.run_coroutine_threadsafe(coro, self._loop).result(timeout)

    def selected(self, case: golden.GoldenCase) -> bool:
        if self.id_filter and case.id not in self.id_filter:
            return False
        return not self.repo_filter or any(g.repo == self.repo_filter
                                           for g in case.gold_sources)

    # ------------------------------------------------------------ lifecycle

    @keyword
    def start_retrieval_services(self) -> None:
        """Start the REST API as its own process and open an MCP stdio session."""
        with socket.socket() as s:
            s.bind(("127.0.0.1", 0))
            port = s.getsockname()[1]
        self._api = subprocess.Popen([sys.executable, "-m", "lab.cli", "serve-api",
                                      "--port", str(port)], cwd=ROOT)
        self._http = httpx.Client(base_url=f"http://127.0.0.1:{port}", timeout=60)
        deadline = time.time() + 60
        while True:
            try:
                if self._http.get("/healthz").status_code == 200:
                    break
            except httpx.HTTPError:
                pass
            if time.time() > deadline:
                raise AssertionError("REST API did not become healthy in 60 s")
            time.sleep(0.5)
        self._mcp = _McpSession(self._loop)
        # Warm-up so model load is not charged to the first golden case.
        self._http.post("/search", json={"query": "warm up", "k": 1})
        logger.info(f"REST on :{port}, MCP over stdio, k={self.k}, "
                    f"ragas={'on (' + self._judge_name() + ')' if self.ragas_enabled else 'off'}")

    @keyword
    def finish_l1_run(self) -> str:
        """Write the JSON score file and stop the services. Returns the file path."""
        try:
            return self._write_scores()
        finally:
            if self._mcp:
                self._mcp.close()
            if self._http:
                self._http.close()
            if self._api:
                self._api.terminate()
                self._api.wait(timeout=10)

    # ------------------------------------------------------------- retrieval

    @keyword
    def search_docs(self, question: str, k: int | None = None) -> dict[str, Any]:
        """Search through the REST API; the response is kept for the following checks."""
        t0 = time.perf_counter()
        r = self._http.post("/search", json={"query": question, "k": int(k or self.k)})
        self.latencies_ms.append((time.perf_counter() - t0) * 1000)
        r.raise_for_status()
        self._last = r.json()
        for i, h in enumerate(self._last["hits"], 1):
            logger.info(f"{i}. {h['score']:.3f} {h['repo']} [{h['origin']}] "
                        f"{h['path']} :: {h['heading']}")
        return self._last

    @keyword("MCP Results Should Match REST")
    def mcp_results_should_match_rest(self) -> None:
        """The MCP search_docs tool returns the same hits, in the same order, as REST."""
        rest = self._last
        mcp = self._mcp.search(rest["query"], rest["k"])
        a = [(h["id"], round(h["score"], 6)) for h in rest["hits"]]
        b = [(h["id"], round(h["score"], 6)) for h in mcp["hits"]]
        if a != b:
            raise AssertionError(f"MCP and REST differ:\nREST {a}\nMCP  {b}")

    # ---------------------------------------------------------------- scoring

    def _record(self, case_id: str) -> dict[str, Any]:
        case = self.cases[case_id]
        if case_id not in self.results or self.results[case_id]["question"] != self._last["query"]:
            self.results[case_id] = l1.evaluate(case, self._last["hits"])
        return self.results[case_id]

    @keyword
    def score_retrieval_with_ragas(self, case_id: str) -> None:
        """Ragas context precision and recall against the expected answer, judged by the
        configured judge LLM. Records scores; never fails the test (thresholds apply to
        the run aggregate)."""
        rec = self._record(case_id)
        if not self.ragas_enabled:
            logger.info("Ragas skipped (no judge configured)")
            return
        from ragas.metrics.collections import ContextPrecision, ContextRecall

        from lab import judge

        if self._ragas_llm is None:
            self._ragas_llm = judge.ragas_llm(self.cfg)
        case = self.cases[case_id]
        contexts = [h["text"] for h in self._last["hits"]]

        async def both():
            return await asyncio.gather(
                ContextPrecision(llm=self._ragas_llm).ascore(
                    user_input=case.question, reference=case.expected_answer,
                    retrieved_contexts=contexts),
                ContextRecall(llm=self._ragas_llm).ascore(
                    user_input=case.question, retrieved_contexts=contexts,
                    reference=case.expected_answer),
            )

        precision, recall = asyncio.run_coroutine_threadsafe(both(), self._loop).result(300)
        rec["ragas_context_precision"] = round(float(precision.value), 4)
        rec["ragas_context_recall"] = round(float(recall.value), 4)
        logger.info(f"Ragas context precision {rec['ragas_context_precision']}, "
                    f"recall {rec['ragas_context_recall']}")

    @keyword
    def retrieval_should_include_gold_source(self, case_id: str) -> None:
        rec = self._record(case_id)
        case = self.cases[case_id]
        if not rec["hit"]:
            got = [f"{h['repo']}:{h['path']}" for h in self._last["hits"]]
            raise AssertionError(
                f"no gold source in top {rec['k']}.\nExpected any of: "
                + "; ".join(map(str, case.gold_sources)) + "\nGot: " + "; ".join(got))
        logger.info(f"gold source at rank {rec['gold_rank']}")

    @keyword
    def trap_source_should_not_outrank_gold(self, case_id: str) -> None:
        """Fails when a stale or misleading document ranks above every gold source."""
        rec = self._record(case_id)
        if rec["trap_above_gold"]:
            raise AssertionError(
                f"trap source at rank {rec['trap_rank']} outranks gold "
                f"(rank {rec['gold_rank'] or 'none'}): the corpus is steering toward a "
                "confident wrong answer")

    @keyword
    def top_hit_should_come_from_expected_origin(self, case_id: str) -> None:
        rec = self._record(case_id)
        if not rec.get("origin_ok", True):
            raise AssertionError(f"top hit is {rec['top_hit']['origin']} "
                                 f"({rec['top_hit']['repo']}), expected {rec['expected_origin']}")

    # ------------------------------------------------------------- run level

    @keyword
    def retrieval_p95_should_be_within_budget(self, budget_ms: float | None = None) -> None:
        budget = float(budget_ms or self.cfg.retrieval.p95_budget_ms)
        p95 = l1.percentile(self.latencies_ms, 95)
        logger.info(f"REST round trip p95 {p95:.1f} ms over {len(self.latencies_ms)} queries")
        if p95 > budget:
            raise AssertionError(f"p95 {p95:.0f} ms exceeds the {budget:.0f} ms budget")

    @keyword
    def l1_aggregates_should_meet_thresholds(self) -> None:
        agg = l1.aggregate(list(self.results.values()), self.latencies_ms)
        t = self.cfg.thresholds
        checks = [("hit_rate", agg["hit_rate"], t.get("l1_hit_rate")),
                  ("mrr", agg["mrr"], t.get("l1_mrr"))]
        if self.ragas_enabled:
            # Ragas context precision is reported, not gated: in M4 calibration it scored
            # 0.0-0.2 where the maintainer graded 0.81-1.0 (docs/notes/M4.md).
            logger.info(f"ragas_context_precision: {agg['ragas_context_precision']} "
                        "(report only)")
            checks += [("ragas_context_recall", agg["ragas_context_recall"],
                        t.get("context_recall"))]
        failures = []
        for name, value, threshold in checks:
            logger.info(f"{name}: {value} (threshold {threshold})")
            if threshold is not None and (value is None or value < threshold):
                failures.append(f"{name} {value} < {threshold}")
        if failures:
            raise AssertionError("; ".join(failures))

    # ---------------------------------------------------------------- report

    def _judge_name(self) -> str:
        ref = self.cfg.models["judge_llm"]
        return f"{ref.provider}:{ref.name}"

    def _index_commits(self) -> dict[str, str]:
        from qdrant_client import QdrantClient
        from qdrant_client.models import FieldCondition, Filter, MatchValue

        from lab.ingest import sources as src

        client = QdrantClient(url=self.cfg.endpoints.qdrant)
        out = {}
        for s in src.load().sources:
            flt = Filter(must=[FieldCondition(key="source_id", match=MatchValue(value=s.id))])
            pts, _ = client.scroll(self.cfg.index.collection, scroll_filter=flt, limit=1,
                                   with_payload=["commit_sha"])
            out[s.id] = pts[0].payload["commit_sha"] if pts else None
        return out

    def _write_scores(self) -> str:
        path = self._write_report("L1", {
            "aggregate": l1.aggregate(list(self.results.values()), self.latencies_ms),
            "cases": list(self.results.values()),
        })
        agg = json.loads(Path(path).read_text(encoding="utf-8"))["aggregate"]
        logger.info(f"hit rate {agg['hit_rate']}, MRR {agg['mrr']}, "
                    f"ragas precision {agg['ragas_context_precision']}, "
                    f"recall {agg['ragas_context_recall']}, p95 {agg['latency_ms']['p95']} ms",
                    also_console=True)
        return path

    def _write_report(self, layer: str, body: dict[str, Any]) -> str:
        """Run metadata shared by every layer, plus the layer's aggregate and cases."""
        from lab import judge

        def git(*args: str) -> str:
            return subprocess.run(["git", *args], check=False, cwd=ROOT, capture_output=True,
                                  text=True).stdout.strip()

        report = {
            "layer": layer,
            "started_at": self.started_at.isoformat(timespec="seconds"),
            "finished_at": datetime.now(UTC).isoformat(timespec="seconds"),
            "lab_commit": git("rev-parse", "--short", "HEAD")
            + ("-dirty" if git("status", "--porcelain") else ""),
            "index": {"collection": self.cfg.index.collection,
                      "embedding_model": self.cfg.models["embedding"].name,
                      "sources": self._index_commits()},
            "k": self.k,
            "judge": self._judge_name() if self.judge_enabled else None,
            "judge_usage": judge.usage_cost(self.cfg) if self.judge_enabled else None,
            "thresholds": {**self.cfg.thresholds,
                           "p95_budget_ms": self.cfg.retrieval.p95_budget_ms},
            **body,
        }
        out_dir = RESULTS_DIR / layer
        out_dir.mkdir(parents=True, exist_ok=True)
        stamp = self.started_at.strftime("%Y%m%dT%H%M%SZ")
        path = out_dir / f"run-{stamp}.json"
        text = json.dumps(report, indent=2)
        path.write_text(text, encoding="utf-8")
        (out_dir / "latest.json").write_text(text, encoding="utf-8")
        logger.info(f"{layer} scores written to {path}; judge {report['judge_usage']}",
                    also_console=True)
        return str(path)
