"""Clone -> select -> chunk -> embed -> upsert, one source at a time."""

from __future__ import annotations

import time
from dataclasses import dataclass
from datetime import UTC, datetime

from qdrant_client import QdrantClient
from qdrant_client.models import PointStruct

from lab import embeddings
from lab.config import LabConfig
from lab.ingest import fetch, index
from lab.ingest import sources as src
from lab.ingest.chunkers import CHUNKER_VERSION, Chunk, chunk_file


@dataclass
class SourceResult:
    source_id: str
    commit_sha: str
    status: str  # indexed | up-to-date
    files: int = 0
    chunks: int = 0
    seconds: float = 0.0


def embed_text(repo: str, chunk: Chunk) -> str:
    """What gets embedded: location and heading context ahead of the body, so a
    section titled 'Hardware' in README.rst is findable by what it is about."""
    return f"{repo} {chunk.path}\n{' > '.join(chunk.heading_path)}\n\n{chunk.text}"


def build_chunks(root, files: list[str], cfg: LabConfig) -> list[Chunk]:
    out: list[Chunk] = []
    for rel in files:
        text = (root / rel).read_text(encoding="utf-8", errors="replace")
        out.extend(chunk_file(rel, text, cfg.ingest.max_chunk_chars, cfg.ingest.min_chunk_chars))
    return out


def ingest_source(
    source: src.Source, exclude: list[str], cfg: LabConfig, client: QdrantClient,
    emb: embeddings.PrefixedEmbedder, force: bool = False,
) -> SourceResult:
    t0 = time.perf_counter()
    root, sha = fetch.checkout(source, cfg.ingest.cache_path())
    key = f"{sha}:{src.fingerprint(source, exclude, CHUNKER_VERSION)}"
    collection = cfg.index.collection
    if not force and index.current_key(client, collection, source.id) == key:
        return SourceResult(source.id, sha, "up-to-date", seconds=time.perf_counter() - t0)

    files = src.select_files(root, source, exclude)
    chunks = build_chunks(root, files, cfg)
    if not chunks:
        raise SystemExit(f"{source.id}: no chunks from {len(files)} files -- check paths")
    vectors = emb.embed_documents([embed_text(source.repo, c) for c in chunks])
    index.ensure_collection(client, collection, len(vectors[0]))

    indexed_at = datetime.now(UTC).isoformat(timespec="seconds")
    seen: dict[str, int] = {}
    points = []
    for chunk, vec in zip(chunks, vectors, strict=True):
        ordinal = seen[chunk.path] = seen.get(chunk.path, -1) + 1
        points.append(PointStruct(
            id=index.point_id(source.id, key, chunk.path, ordinal),
            vector=vec,
            payload={
                "source_id": source.id,
                "ingest_key": key,
                "repo": source.repo,
                "origin": source.origin,
                "branch": source.branch,
                "commit_sha": sha,
                "path": chunk.path,
                "doc_type": chunk.doc_type,
                "heading_path": list(chunk.heading_path),
                "heading": " > ".join(chunk.heading_path),
                "chunk_index": ordinal,
                "part": chunk.part,
                "text": chunk.text,
                "url": f"https://github.com/{source.repo}/blob/{sha}/{chunk.path}",
                "embedding_model": emb.name,
                "indexed_at": indexed_at,
            },
        ))
    index.upsert(client, collection, points)
    index.delete_stale(client, collection, source.id, key)
    return SourceResult(source.id, sha, "indexed", len(files), len(chunks),
                        time.perf_counter() - t0)


def ingest_all(cfg: LabConfig, sources_cfg: src.SourcesConfig, only: list[str] | None = None,
               force: bool = False, recreate: bool = False, log=print) -> list[SourceResult]:
    client = QdrantClient(url=cfg.endpoints.qdrant)
    emb = embeddings.from_config(cfg)
    if recreate and client.collection_exists(cfg.index.collection):
        client.delete_collection(cfg.index.collection)
        log(f"dropped collection {cfg.index.collection}")
    results = []
    for source in sources_cfg.sources:
        if only and source.id not in only and source.repo not in only:
            continue
        r = ingest_source(source, sources_cfg.exclude, cfg, client, emb, force=force)
        log(f"{r.status:<10} {r.source_id:<36} {r.commit_sha[:7]}  "
            f"{r.files:>4} files {r.chunks:>5} chunks  {r.seconds:6.1f}s")
        results.append(r)
    if not only:
        pruned = index.prune_sources(client, cfg.index.collection,
                                     [s.id for s in sources_cfg.sources])
        if pruned:
            log(f"pruned {pruned} points from sources no longer configured")
    return results
