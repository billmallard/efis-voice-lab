"""`lab` command line: ingest, query, stats, serve-api, serve-mcp, ask."""

from __future__ import annotations

import argparse
import sys
import textwrap
import time
from collections import Counter

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from lab import config, embeddings
from lab.ingest import pipeline
from lab.ingest import sources as src


def cmd_ingest(args: argparse.Namespace) -> int:
    cfg = config.load()
    pipeline.ingest_all(cfg, src.load(), only=args.source, force=args.force,
                        recreate=args.recreate)
    return 0


def cmd_query(args: argparse.Namespace) -> int:
    cfg = config.load()
    client = QdrantClient(url=cfg.endpoints.qdrant)
    emb = embeddings.from_config(cfg)
    must = [FieldCondition(key=k, match=MatchValue(value=v))
            for k, v in (("origin", args.origin), ("repo", args.repo)) if v]
    t0 = time.perf_counter()
    hits = client.query_points(
        cfg.index.collection, query=emb.embed_query(args.question),
        query_filter=Filter(must=must) if must else None,
        limit=args.k or cfg.retrieval.top_k,
    ).points
    ms = (time.perf_counter() - t0) * 1000
    print(f"{len(hits)} hits in {ms:.0f} ms for {args.question!r}\n")
    for rank, h in enumerate(hits, 1):
        p = h.payload
        print(f"{rank}. {h.score:.3f}  {p['repo']} [{p['origin']}] @{p['commit_sha'][:7]}  "
              f"{p['path']}")
        print(f"   {p['heading']}")
        body = " ".join(p["text"].split())
        print(textwrap.indent(textwrap.fill(body[: args.chars], 96), "   "), end="\n\n")
    return 0


def cmd_stats(args: argparse.Namespace) -> int:
    cfg = config.load()
    client = QdrantClient(url=cfg.endpoints.qdrant)
    name = cfg.index.collection
    if not client.collection_exists(name):
        print(f"collection {name!r} does not exist -- run `lab ingest`")
        return 1
    print(f"collection {name}: {client.count(name, exact=True).count} points")
    for s in src.load().sources:
        flt = Filter(must=[FieldCondition(key="source_id", match=MatchValue(value=s.id))])
        n = client.count(name, count_filter=flt, exact=True).count
        types: Counter[str] = Counter()
        sha = "-"
        offset = None
        while True:
            pts, offset = client.scroll(name, scroll_filter=flt, limit=1000, offset=offset,
                                        with_payload=["doc_type", "commit_sha"])
            for p in pts:
                types[p.payload["doc_type"]] += 1
                sha = p.payload["commit_sha"][:7]
            if offset is None:
                break
        detail = ", ".join(f"{t} {c}" for t, c in sorted(types.items()))
        print(f"  {s.id:<36} {s.origin:<9} @{sha}  {n:>5}  ({detail})")
    return 0


def cmd_serve_api(args: argparse.Namespace) -> int:
    import uvicorn

    uvicorn.run("lab.retrieval.rest_api:create_app", factory=True, host=args.host,
                port=args.port, log_level="warning")
    return 0


def cmd_serve_mcp(args: argparse.Namespace) -> int:
    from lab.retrieval import mcp_server

    mcp_server.main(http_port=args.http)
    return 0


def cmd_ask(args: argparse.Namespace) -> int:
    import asyncio
    import json

    from lab.agent.text_agent import open_agent
    from lab.harness import golden

    cfg = config.load()
    if args.model:
        cfg.models["agent_llm"].name = args.model
    cases = {c.id: c for c in golden.load()}
    questions = [cases[q].question if q in cases else q for q in args.question]

    async def run() -> None:
        async with open_agent(cfg) as agent:
            for q in questions:
                turn = await agent.ask(q)
                if args.json:
                    print(json.dumps(turn.model_dump(exclude={"contexts"}), indent=2))
                    continue
                print(f"Q: {q}")
                for c in turn.tool_calls:
                    top = "; ".join(f"{h['repo'].split('/')[1]}:{h['path']}" for h in c.hits[:3])
                    print(f"   -> {c.name}({json.dumps(c.args)}) {c.ms:.0f} ms"
                          + (f" ERROR {c.error}" if c.error else f"  [{top}]"))
                print(f"A: {turn.answer}")
                if turn.spoken != turn.answer:
                    print(f"S: {turn.spoken}")
                t = turn.timing
                print(f"   {turn.model}, {turn.rounds} rounds, stop={turn.stop}, "
                      f"{t['total_ms']:.0f} ms (llm {t['llm_ms']:.0f}, "
                      f"first {t['first_llm_ms']:.0f}), {turn.usage['input_tokens']} in / "
                      f"{turn.usage['output_tokens']} out\n")

    asyncio.run(run())
    return 0


def cmd_calibrate(args: argparse.Namespace) -> int:
    import json
    from pathlib import Path

    from lab.harness import calibration

    if args.action == "export":
        run = Path(args.path or config.ROOT / "results" / "L2" / "latest.json")
        out = calibration.export(config.load(), run, n=args.n,
                                 l1_baseline=Path(args.l1_baseline) if args.l1_baseline
                                 else None)
        print(f"worksheet: {out}")
        return 0
    if args.action == "rejudge":
        out = calibration.rejudge(config.load(), Path(args.path), args.judges.split(","),
                                  set(args.ids.split(",")) if args.ids else None)
        print(f"rejudged run: {out}")
        return 0
    report = calibration.score(Path(args.path), Path(args.run) if args.run else None)
    print(json.dumps(report, indent=2))
    return 0


def cmd_serve_voice(args: argparse.Namespace) -> int:
    import runpy

    # The Pipecat runner looks for bot() on __main__, so run serve.py as __main__.
    sys.argv = [sys.argv[0], args.host, str(args.port)]
    runpy.run_module("lab.voice.serve", run_name="__main__", alter_sys=True)
    return 0


def cmd_report(args: argparse.Namespace) -> int:
    from lab.harness import report

    print(f"report: {report.build(config.load())}")
    return 0


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(prog="lab", description=__doc__)
    sub = ap.add_subparsers(dest="cmd", required=True)

    p = sub.add_parser("ingest", help="clone sources, chunk, embed, upsert")
    p.add_argument("--source", action="append",
                   help="only this source (repo or repo@branch); repeatable")
    p.add_argument("--force", action="store_true", help="re-index even if up to date")
    p.add_argument("--recreate", action="store_true", help="drop the collection first")
    p.set_defaults(fn=cmd_ingest)

    p = sub.add_parser("query", help="search the index")
    p.add_argument("question")
    p.add_argument("-k", type=int, help="number of hits (default: retrieval.top_k)")
    p.add_argument("--origin", choices=["fork", "upstream"])
    p.add_argument("--repo")
    p.add_argument("--chars", type=int, default=300, help="body characters to show")
    p.set_defaults(fn=cmd_query)

    p = sub.add_parser("stats", help="points per source")
    p.set_defaults(fn=cmd_stats)

    p = sub.add_parser("serve-api", help="REST retrieval API (POST /search, GET /source)")
    p.add_argument("--host", default="127.0.0.1")
    p.add_argument("--port", type=int, default=8765)
    p.set_defaults(fn=cmd_serve_api)

    p = sub.add_parser("serve-mcp", help="MCP server (stdio unless --http)")
    p.add_argument("--http", type=int, metavar="PORT", help="serve streamable HTTP instead")
    p.set_defaults(fn=cmd_serve_mcp)

    p = sub.add_parser("ask", help="ask the text agent (question text or golden id)")
    p.add_argument("question", nargs="+", help="question text, or golden ids such as HW-003")
    p.add_argument("--model", help="agent LLM name (default: models.agent_llm.name)")
    p.add_argument("--json", action="store_true", help="print the full AgentTurn")
    p.set_defaults(fn=cmd_ask)

    p = sub.add_parser("calibrate", help="judge calibration worksheet: export, then score")
    p.add_argument("action", choices=["export", "score", "rejudge"])
    p.add_argument("path", nargs="?",
                   help="export: L2 run JSON (default results/L2/latest.json); "
                        "score: the filled-in worksheet; rejudge: the run JSON")
    p.add_argument("-n", type=int, default=20, help="cases to sample (export)")
    p.add_argument("--l1-baseline", help="L1 run JSON with the Ragas precision to compare")
    p.add_argument("--run", help="score: compare against this run instead of the worksheet's")
    p.add_argument("--judges", default="policy,correctness,speakability",
                   help="rejudge: comma-separated judges to re-run")
    p.add_argument("--ids", help="rejudge: only these case ids")
    p.set_defaults(fn=cmd_calibrate)

    p = sub.add_parser("serve-voice", help="browser demo: talk to the voice agent (WebRTC)")
    p.add_argument("--host", default="localhost")
    p.add_argument("--port", type=int, default=7860)
    p.set_defaults(fn=cmd_serve_voice)

    p = sub.add_parser("report", help="static HTML report from the latest L1/L2/L3 runs")
    p.set_defaults(fn=cmd_report)

    args = ap.parse_args(argv)
    # Chunk text is full of Unicode; a Windows console defaults to cp1252.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
