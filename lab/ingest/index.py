"""Qdrant collection management for the docs index.

Idempotency: every point carries `source_id` and `ingest_key` (commit sha +
source-config fingerprint). Re-ingesting a source whose key is already in the
index is a no-op; a new key upserts the new points first, then deletes the
source's old ones, so the source is never missing from the index mid-update.
Point ids are uuid5 of (source, key, path, section, part), so re-running the
same ingest overwrites in place.
"""

from __future__ import annotations

import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchAny,
    MatchValue,
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

KEYWORD_FIELDS = ("source_id", "ingest_key", "repo", "origin", "path", "commit_sha", "doc_type")
_NS = uuid.UUID("6f1c1d9e-5b7a-4f43-9a51-2a3c0b8e7d10")


def point_id(*parts: object) -> str:
    return str(uuid.uuid5(_NS, "\x1f".join(str(p) for p in parts)))


def ensure_collection(client: QdrantClient, name: str, dim: int) -> None:
    if client.collection_exists(name):
        params = client.get_collection(name).config.params.vectors
        if params.size != dim:
            raise SystemExit(
                f"collection {name!r} has dim {params.size}, embedder gives {dim}. "
                "The embedding model changed: re-run ingest with --recreate."
            )
        return
    client.create_collection(name, vectors_config=VectorParams(size=dim, distance=Distance.COSINE))
    for f in KEYWORD_FIELDS:
        client.create_payload_index(name, f, field_schema=PayloadSchemaType.KEYWORD)


def _match(**fields: str) -> list[FieldCondition]:
    return [FieldCondition(key=k, match=MatchValue(value=v)) for k, v in fields.items()]


def current_key(client: QdrantClient, name: str, source_id: str) -> str | None:
    """The ingest_key of a source's points, or None if it has none (or several)."""
    if not client.collection_exists(name):
        return None
    flt = Filter(must=_match(source_id=source_id))
    points, _ = client.scroll(name, scroll_filter=flt, limit=1, with_payload=["ingest_key"])
    if not points:
        return None
    key = points[0].payload["ingest_key"]
    stale = Filter(must=_match(source_id=source_id),
                   must_not=_match(ingest_key=key))
    return None if client.count(name, count_filter=stale, exact=True).count else key


def upsert(client: QdrantClient, name: str, points: list[PointStruct], batch: int = 256) -> None:
    for i in range(0, len(points), batch):
        client.upsert(name, points=points[i : i + batch], wait=True)


def delete_stale(client: QdrantClient, name: str, source_id: str, keep_key: str) -> None:
    flt = Filter(must=_match(source_id=source_id), must_not=_match(ingest_key=keep_key))
    client.delete(name, points_selector=FilterSelector(filter=flt), wait=True)


def prune_sources(client: QdrantClient, name: str, keep_ids: list[str]) -> int:
    """Delete points of sources no longer in sources.yaml; return how many."""
    if not client.collection_exists(name):
        return 0
    flt = Filter(must_not=[FieldCondition(key="source_id", match=MatchAny(any=keep_ids))])
    n = client.count(name, count_filter=flt, exact=True).count
    if n:
        client.delete(name, points_selector=FilterSelector(filter=flt), wait=True)
    return n
