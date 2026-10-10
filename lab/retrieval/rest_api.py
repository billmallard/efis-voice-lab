"""REST interface over the shared Retriever, for hosted agent platforms and direct testing.

Run with `lab serve-api`. If LAB_API_TOKEN is set, every route except /healthz
requires `Authorization: Bearer <token>` -- needed before the API is exposed
through a tunnel (M8, D2).
"""

from __future__ import annotations

import hmac
import os
from collections.abc import Callable

from fastapi import Depends, FastAPI, Header, HTTPException, Query
from pydantic import BaseModel, Field

from lab.retrieval.core import MAX_K, Retriever, SearchFilters, SearchResponse, SourceDocument


class SearchRequest(BaseModel):
    query: str = Field(min_length=1)
    k: int = Field(5, ge=1, le=MAX_K)
    filters: SearchFilters = SearchFilters()


def build(retriever_factory: Callable[[], Retriever], token: str | None = None) -> FastAPI:
    app = FastAPI(title="EFIS Voice Lab retrieval", version="1")
    state: dict[str, Retriever] = {}

    def retriever() -> Retriever:
        if "r" not in state:
            state["r"] = retriever_factory()
        return state["r"]

    def auth(authorization: str | None = Header(None)) -> None:
        if token and not hmac.compare_digest(authorization or "", f"Bearer {token}"):
            raise HTTPException(401, "missing or invalid bearer token")

    @app.get("/healthz")
    def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.post("/search", dependencies=[Depends(auth)])
    def search(req: SearchRequest) -> SearchResponse:
        return retriever().search(req.query, req.k, req.filters)

    @app.get("/source", dependencies=[Depends(auth)])
    def source(repo: str = Query(...), path: str = Query(...)) -> SourceDocument:
        doc = retriever().get_source(repo, path)
        if doc is None:
            raise HTTPException(404, f"{repo}:{path} is not in the index")
        return doc

    return app


def create_app() -> FastAPI:
    """uvicorn factory: `uvicorn --factory lab.retrieval.rest_api:create_app`."""
    from lab import config

    return build(lambda: Retriever(config.load()), token=os.environ.get("LAB_API_TOKEN"))
