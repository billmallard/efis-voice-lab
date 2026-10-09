"""`lab` command line: ingest, query, stats."""

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

    args = ap.parse_args(argv)
    # Chunk text is full of Unicode; a Windows console defaults to cp1252.
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return args.fn(args)


if __name__ == "__main__":
    sys.exit(main())
