"""M1 acceptance: the index holds every configured source, every chunk carries
complete metadata, excluded files are absent, and a few probe queries land on
the right document.

Exits nonzero on any failure. `--sources` limits the check to a subset (CI
indexes only the small sources).
"""

from __future__ import annotations

import argparse
import re
import sys
from collections import Counter
from datetime import datetime

from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from lab import config, embeddings
from lab.ingest import sources as src

REQUIRED = ("repo", "origin", "branch", "commit_sha", "path", "heading_path", "indexed_at",
            "source_id", "doc_type", "text", "url", "embedding_model")
SHA = re.compile(r"^[0-9a-f]{40}$")

# (question, source repo, expected path in top 5)
PROBES = [
    ("How do I write a FIX Gateway plugin?", "makerplane/FIX-Gateway", "doc/plugin.rst"),
    ("What does the netfix ASCII protocol look like?", "makerplane/FIX-Gateway",
     "doc/appendix/netfixascii.rst"),
    ("How do I set the altimeter barometric setting?", "billmallard/pyEfis",
     "docs/wiki/Pilots-Guide.md"),
    ("What hardware do I need for synthetic vision?", "billmallard/pyEfis", "README.rst"),
]


def problems_in(payload: dict, source: src.Source, exclude: list[str]) -> list[str]:
    out = [f"missing {k}" for k in REQUIRED if payload.get(k) in (None, "", [])]
    if out:
        return out
    if not SHA.match(payload["commit_sha"]):
        out.append(f"bad commit_sha {payload['commit_sha']!r}")
    if payload["origin"] != source.origin or payload["branch"] != source.branch:
        out.append("origin/branch disagree with sources.yaml")
    try:
        datetime.fromisoformat(payload["indexed_at"])
    except ValueError:
        out.append(f"bad indexed_at {payload['indexed_at']!r}")
    if src.is_excluded(payload["path"], exclude):
        out.append("EXCLUDED FILE INDEXED")
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sources", nargs="*", help="repos to check (default: all configured)")
    args = ap.parse_args()

    cfg = config.load()
    sc = src.load()
    client = QdrantClient(url=cfg.endpoints.qdrant)
    name = cfg.index.collection
    wanted = [s for s in sc.sources if not args.sources or s.repo in args.sources]
    failures: list[str] = []

    for s in wanted:
        flt = Filter(must=[FieldCondition(key="source_id", match=MatchValue(value=s.id))])
        n = 0
        bad: Counter[str] = Counter()
        offset = None
        while True:
            pts, offset = client.scroll(name, scroll_filter=flt, limit=500, offset=offset)
            for p in pts:
                n += 1
                for problem in problems_in(p.payload, s, sc.exclude):
                    bad[problem] += 1
            if offset is None:
                break
        status = "ok  " if n and not bad else "FAIL"
        print(f"{status} {s.id:<36} {n:>5} chunks")
        if not n:
            failures.append(f"{s.id}: no chunks indexed")
        for problem, count in bad.items():
            failures.append(f"{s.id}: {count} chunks: {problem}")

    emb = embeddings.from_config(cfg)
    repos = {s.repo for s in wanted}
    for question, repo, expected in PROBES:
        if repo not in repos:
            continue
        flt = Filter(must=[FieldCondition(key="repo", match=MatchValue(value=repo))])
        hits = client.query_points(name, query=emb.embed_query(question), query_filter=flt,
                                   limit=5).points
        paths = [h.payload["path"] for h in hits]
        ok = expected in paths
        rank = paths.index(expected) + 1 if ok else "-"
        print(f"{'ok  ' if ok else 'FAIL'} probe rank {rank}: {question!r} -> {expected}")
        if not ok:
            failures.append(f"probe {question!r}: {expected} not in top 5 {paths}")

    for f in failures:
        print("  " + f)
    print("PASS" if not failures else f"{len(failures)} failure(s)")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(main())
