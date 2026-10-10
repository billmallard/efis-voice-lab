"""Search logic shared by the MCP server and the REST API.

Both interfaces are thin adapters over `Retriever`, so "MCP and REST return
identical results" holds by construction and is verified end to end by
scripts/check_retrieval.py.
"""

from __future__ import annotations

import time
from typing import Literal

from pydantic import BaseModel, Field
from qdrant_client import QdrantClient
from qdrant_client.models import FieldCondition, Filter, MatchValue

from lab import embeddings
from lab.config import LabConfig

MAX_K = 20


class SearchFilters(BaseModel):
    origin: Literal["fork", "upstream"] | None = Field(
        None, description="fork = billmallard/*, upstream = makerplane/*")
    repo: str | None = Field(None, description="owner/name, e.g. makerplane/FIX-Gateway")
    doc_type: Literal["markdown", "rst", "yaml"] | None = None

    def to_qdrant(self) -> Filter | None:
        must = [FieldCondition(key=k, match=MatchValue(value=v))
                for k, v in self.model_dump(exclude_none=True).items()]
        return Filter(must=must) if must else None


class Hit(BaseModel):
    id: str
    score: float
    repo: str
    origin: str
    branch: str
    commit_sha: str
    path: str
    heading: str
    heading_path: list[str]
    chunk_index: int
    text: str
    url: str


class Timing(BaseModel):
    embed_ms: float
    search_ms: float
    total_ms: float


class SearchResponse(BaseModel):
    query: str
    k: int
    filters: SearchFilters
    hits: list[Hit]
    embedding_model: str
    collection: str
    timing: Timing


class Section(BaseModel):
    heading: str
    text: str


class SourceDocument(BaseModel):
    repo: str
    origin: str
    path: str
    commit_sha: str
    url: str
    sections: list[Section]


class Retriever:
    def __init__(self, cfg: LabConfig, client: QdrantClient | None = None,
                 embedder: embeddings.PrefixedEmbedder | None = None):
        self.cfg = cfg
        self.collection = cfg.index.collection
        self.client = client or QdrantClient(url=cfg.endpoints.qdrant)
        self.embedder = embedder or embeddings.from_config(cfg)

    def search(self, query: str, k: int | None = None,
               filters: SearchFilters | None = None) -> SearchResponse:
        k = max(1, min(k or self.cfg.retrieval.top_k, MAX_K))
        filters = filters or SearchFilters()
        t0 = time.perf_counter()
        vector = self.embedder.embed_query(query)
        t1 = time.perf_counter()
        points = self.client.query_points(
            self.collection, query=vector, query_filter=filters.to_qdrant(), limit=k,
        ).points
        t2 = time.perf_counter()
        # Stable order for equal scores, so the two interfaces can be compared exactly.
        points.sort(key=lambda p: (-p.score, str(p.id)))
        hits = [Hit(id=str(p.id), score=p.score, **{f: p.payload[f] for f in _HIT_FIELDS})
                for p in points]
        return SearchResponse(
            query=query, k=k, filters=filters, hits=hits,
            embedding_model=self.embedder.name, collection=self.collection,
            timing=Timing(embed_ms=(t1 - t0) * 1000, search_ms=(t2 - t1) * 1000,
                          total_ms=(t2 - t0) * 1000),
        )

    def get_source(self, repo: str, path: str) -> SourceDocument | None:
        """Every indexed section of one document, in document order."""
        flt = Filter(must=[FieldCondition(key="repo", match=MatchValue(value=repo)),
                           FieldCondition(key="path", match=MatchValue(value=path))])
        points, offset = [], None
        while True:
            batch, offset = self.client.scroll(self.collection, scroll_filter=flt, limit=256,
                                               offset=offset)
            points.extend(batch)
            if offset is None:
                break
        if not points:
            return None
        points.sort(key=lambda p: p.payload["chunk_index"])
        first = points[0].payload
        return SourceDocument(
            repo=repo, origin=first["origin"], path=path, commit_sha=first["commit_sha"],
            url=first["url"],
            sections=[Section(heading=p.payload["heading"], text=p.payload["text"])
                      for p in points],
        )


_HIT_FIELDS = ("repo", "origin", "branch", "commit_sha", "path", "heading", "heading_path",
               "chunk_index", "text", "url")
