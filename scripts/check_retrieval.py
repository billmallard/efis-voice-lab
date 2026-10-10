"""M2 acceptance: the REST API and the MCP server, each running as a real process,
return identical results for the same queries, and p95 latency is within budget.

- REST: `lab serve-api` (uvicorn) over HTTP.
- MCP: `lab serve-mcp` over stdio, the way an MCP client such as Claude Code runs it.

Exits nonzero on any mismatch or a p95 over budget (config retrieval.p95_budget_ms,
overridable with --budget-ms). `--repo` limits queries to one repo (CI indexes
only the small sources).
"""

from __future__ import annotations

import argparse
import asyncio
import statistics
import subprocess
import sys
import time

import httpx
from fastmcp import Client
from fastmcp.client.transports import StdioTransport

from lab import config
from lab.config import ROOT

QUESTIONS = [
    "Where does pyEfis get its flight data?",
    "What's the reference hardware for pyEfis?",
    "What's the minimum hardware to run pyEfis?",
    "Do I need a GPU to run pyEfis?",
    "What happens if synthetic vision is on but there's no usable GPU?",
    "How much storage does the North America terrain set need?",
    "How do I change the altimeter setting?",
    "How do I change the airspeed mode?",
    "How do I switch screens?",
    "Is synthetic vision on by default, and how do I enable it?",
    "Where does the SVS terrain data come from?",
    "How do I set up virtual VFR chart data?",
    "Which install extras are required?",
    "How do I run the tests?",
    "What does the DATA annunciator mean?",
    "What license is pyEfis under?",
    "How do I write a FIX Gateway plugin?",
    "What does the netfix ASCII protocol look like?",
    "How does FIX Gateway talk to X-Plane?",
    "What is the FIX Gateway database?",
]
SCORE_TOL = 1e-6


def percentile(values: list[float], pct: float) -> float:
    return statistics.quantiles(values, n=100, method="inclusive")[int(pct) - 1]


def same(a: dict, b: dict) -> str | None:
    ha, hb = a["hits"], b["hits"]
    if [h["id"] for h in ha] != [h["id"] for h in hb]:
        return f"hit ids/order differ: {[h['path'] for h in ha]} vs {[h['path'] for h in hb]}"
    for x, y in zip(ha, hb, strict=True):
        if abs(x["score"] - y["score"]) > SCORE_TOL:
            return f"score differs for {x['path']}: {x['score']} vs {y['score']}"
        if {k: v for k, v in x.items() if k != "score"} != {k: v for k, v in y.items()
                                                           if k != "score"}:
            return f"payload differs for {x['path']}"
    return None


def wait_healthy(url: str, timeout: float = 60) -> None:
    deadline = time.time() + timeout
    while time.time() < deadline:
        try:
            if httpx.get(f"{url}/healthz", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.5)
    raise SystemExit("REST API did not become healthy")


async def run(args: argparse.Namespace, base: str, budget_ms: float) -> int:
    failures: list[str] = []
    transport = StdioTransport(sys.executable, ["-m", "lab.cli", "serve-mcp"], cwd=str(ROOT))
    async with Client(transport) as mcp, httpx.AsyncClient(base_url=base, timeout=30) as http:
        def body(q):
            return {"query": q, "k": args.k,
                    "filters": {"repo": args.repo} if args.repo else {}}

        def tool_args(q):
            return {"query": q, "k": args.k, **({"repo": args.repo} if args.repo else {})}

        # Warm both paths (model load, connection setup) before timing.
        await http.post("/search", json=body(QUESTIONS[0]))
        await mcp.call_tool("search_docs", tool_args(QUESTIONS[0]))

        rest_rtt, mcp_rtt, server_ms = [], [], []
        for q in QUESTIONS:
            t0 = time.perf_counter()
            r = (await http.post("/search", json=body(q))).json()
            rest_rtt.append((time.perf_counter() - t0) * 1000)
            t0 = time.perf_counter()
            m = (await mcp.call_tool("search_docs", tool_args(q))).structured_content
            mcp_rtt.append((time.perf_counter() - t0) * 1000)
            server_ms += [r["timing"]["total_ms"], m["timing"]["total_ms"]]
            diff = same(r, m)
            top = r["hits"][0]
            print(f"{'ok  ' if not diff else 'FAIL'} {q[:58]:<58} -> "
                  f"{top['repo'].split('/')[1]}:{top['path']} ({top['score']:.3f})")
            if diff:
                failures.append(f"{q!r}: {diff}")

        # Extra REST passes so p95 rests on more than one sample per query.
        for _ in range(args.repeat - 1):
            for q in QUESTIONS:
                t0 = time.perf_counter()
                await http.post("/search", json=body(q))
                rest_rtt.append((time.perf_counter() - t0) * 1000)

        src = (await mcp.call_tool("get_source", {"repo": top["repo"],
                                                  "path": top["path"]})).structured_content
        via_rest = (await http.get("/source", params={"repo": top["repo"],
                                                      "path": top["path"]})).json()
        if src != via_rest:
            failures.append("get_source differs between MCP and REST")
        print(f"{'ok  ' if src == via_rest else 'FAIL'} get_source {top['path']}: "
              f"{len(src['sections'])} sections, identical on both interfaces")

    print()
    for name, xs in (("server-side search", server_ms), ("REST round trip", rest_rtt),
                     ("MCP round trip", mcp_rtt)):
        print(f"{name:<19} n={len(xs):<4} p50={percentile(xs, 50):6.1f} ms  "
              f"p95={percentile(xs, 95):6.1f} ms  max={max(xs):6.1f} ms")
    p95 = percentile(rest_rtt, 95)
    if p95 > budget_ms:
        failures.append(f"REST p95 {p95:.0f} ms over the {budget_ms:.0f} ms budget")
    print(f"budget: REST p95 <= {budget_ms:.0f} ms")
    for f in failures:
        print("  " + f)
    print("PASS" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--port", type=int, default=8799)
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--repeat", type=int, default=5, help="REST passes over the questions")
    ap.add_argument("--repo", help="restrict every query to this repo")
    ap.add_argument("--budget-ms", type=float)
    args = ap.parse_args()
    budget = args.budget_ms or config.load().retrieval.p95_budget_ms
    base = f"http://127.0.0.1:{args.port}"
    api = subprocess.Popen([sys.executable, "-m", "lab.cli", "serve-api", "--port",
                            str(args.port)], cwd=ROOT)
    try:
        wait_healthy(base)
        return asyncio.run(run(args, base, budget))
    finally:
        api.terminate()
        api.wait(timeout=10)


if __name__ == "__main__":
    sys.exit(main())
