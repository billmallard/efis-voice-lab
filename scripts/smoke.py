"""M0 smoke test: embed a few sentences, store them in Qdrant, retrieve by paraphrase.

Exits nonzero on failure. Requires `docker compose up -d --wait`.
"""

from __future__ import annotations

import sys
import time
import uuid

from qdrant_client import QdrantClient
from qdrant_client.models import Distance, PointStruct, VectorParams

from lab import config, embeddings

DOCS = [
    "pyEfis gets its flight data from FIX Gateway rather than reading hardware directly.",
    "Synthetic vision needs a usable GPU; the core instruments are drawn on the CPU.",
    "The left and right square bracket keys change the altimeter setting.",
]
QUERY = "Do I need a graphics card to run this?"
EXPECTED = 1


def main() -> int:
    cfg = config.load()
    emb = embeddings.from_config(cfg)
    qdrant = QdrantClient(url=cfg.endpoints.qdrant)
    collection = f"smoke_{uuid.uuid4().hex[:8]}"

    vectors = emb.embed(DOCS)
    dim = len(vectors[0])
    print(f"embedding model {emb.name}: {len(vectors)} vectors, dim {dim}")

    qdrant.create_collection(
        collection, vectors_config=VectorParams(size=dim, distance=Distance.COSINE)
    )
    try:
        qdrant.upsert(
            collection,
            points=[
                PointStruct(id=i, vector=v, payload={"text": t})
                for i, (t, v) in enumerate(zip(DOCS, vectors))
            ],
            wait=True,
        )
        t0 = time.perf_counter()
        hits = qdrant.query_points(collection, query=emb.embed([QUERY])[0], limit=3).points
        ms = (time.perf_counter() - t0) * 1000
        print(f"query: {QUERY!r} ({ms:.0f} ms incl. embedding)")
        for h in hits:
            print(f"  {h.score:.3f}  #{h.id}  {h.payload['text']}")
        if hits[0].id != EXPECTED:
            print(f"FAIL: expected #{EXPECTED} on top, got #{hits[0].id}")
            return 1
        print("PASS")
        return 0
    finally:
        qdrant.delete_collection(collection)


if __name__ == "__main__":
    sys.exit(main())
