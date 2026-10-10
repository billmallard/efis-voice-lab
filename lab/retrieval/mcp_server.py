"""MCP interface: `search_docs` and `get_source` tools over the shared Retriever.

Run with `lab serve-mcp` (stdio, for MCP clients such as Claude Code) or
`lab serve-mcp --http 8766` (streamable HTTP).
"""

from __future__ import annotations

from typing import Annotated, Literal

from fastmcp import FastMCP
from fastmcp.exceptions import ToolError
from pydantic import Field

from lab.retrieval.core import MAX_K, Retriever, SearchFilters, SearchResponse, SourceDocument

INSTRUCTIONS = (
    "Search the pyEfis / FIX-Gateway / MakerPlane documentation. Use search_docs to find "
    "relevant sections, then get_source to read a whole document. Hits from origin 'fork' "
    "(billmallard/*) and 'upstream' (makerplane/*) can disagree: say which one an answer "
    "comes from."
)


def build(retriever_factory) -> FastMCP:
    """`retriever_factory` is called lazily so the server starts without Qdrant/Ollama."""
    mcp = FastMCP("efis-docs", instructions=INSTRUCTIONS)
    state: dict[str, Retriever] = {}

    def retriever() -> Retriever:
        if "r" not in state:
            state["r"] = retriever_factory()
        return state["r"]

    @mcp.tool
    def search_docs(
        query: Annotated[str, Field(description="Natural-language question or keywords")],
        k: Annotated[int, Field(ge=1, le=MAX_K, description="Number of hits")] = 5,
        origin: Annotated[Literal["fork", "upstream"] | None,
                          Field(description="Restrict to the fork or to upstream")] = None,
        repo: Annotated[str | None, Field(description="Restrict to one repo, owner/name")] = None,
    ) -> SearchResponse:
        """Semantic search over the indexed documentation. Returns ranked sections with
        their repo, origin, commit, path, heading and a GitHub URL."""
        return retriever().search(query, k, SearchFilters(origin=origin, repo=repo))

    @mcp.tool
    def get_source(
        repo: Annotated[str, Field(description="owner/name, as returned by search_docs")],
        path: Annotated[str, Field(description="Repo-relative path, as returned by search_docs")],
    ) -> SourceDocument:
        """All indexed sections of one document, in order."""
        doc = retriever().get_source(repo, path)
        if doc is None:
            # ToolError is an expected, client-facing failure: no server-side traceback.
            raise ToolError(f"{repo}:{path} is not in the index")
        return doc

    return mcp


def main(http_port: int | None = None) -> None:
    from lab import config

    server = build(lambda: Retriever(config.load()))
    if http_port:
        server.run(transport="http", host="127.0.0.1", port=http_port, show_banner=False)
    else:
        server.run(transport="stdio", show_banner=False)
