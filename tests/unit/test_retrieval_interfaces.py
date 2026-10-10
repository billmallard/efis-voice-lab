"""Both interfaces against a fake retriever: no Qdrant or Ollama needed."""

import asyncio

import pytest
from fastapi.testclient import TestClient
from fastmcp import Client
from fastmcp.exceptions import ToolError

from lab.retrieval import mcp_server, rest_api
from lab.retrieval.core import (
    Hit,
    SearchFilters,
    SearchResponse,
    Section,
    SourceDocument,
    Timing,
)


class FakeRetriever:
    def __init__(self):
        self.calls = []

    def search(self, query, k=None, filters=None):
        self.calls.append((query, k, filters))
        hit = Hit(id="1", score=0.9, repo="o/r", origin="fork", branch="main",
                  commit_sha="a" * 40, path="README.md", heading="Intro",
                  heading_path=["Intro"], chunk_index=0, text="hello", url="https://x")
        return SearchResponse(query=query, k=k or 5, filters=filters or SearchFilters(),
                              hits=[hit], embedding_model="fake", collection="c",
                              timing=Timing(embed_ms=1, search_ms=1, total_ms=2))

    def get_source(self, repo, path):
        if path != "README.md":
            return None
        return SourceDocument(repo=repo, origin="fork", path=path, commit_sha="a" * 40,
                              url="https://x", sections=[Section(heading="Intro", text="hello")])


@pytest.fixture
def fake():
    return FakeRetriever()


def test_rest_search_and_source(fake):
    client = TestClient(rest_api.build(lambda: fake))
    r = client.post("/search", json={"query": "q", "k": 3, "filters": {"origin": "fork"}})
    assert r.status_code == 200
    assert r.json()["hits"][0]["path"] == "README.md"
    assert fake.calls[-1] == ("q", 3, SearchFilters(origin="fork"))
    assert client.get("/source", params={"repo": "o/r", "path": "README.md"}).status_code == 200
    assert client.get("/source", params={"repo": "o/r", "path": "nope.md"}).status_code == 404


def test_rest_validates_input(fake):
    client = TestClient(rest_api.build(lambda: fake))
    assert client.post("/search", json={"query": ""}).status_code == 422
    assert client.post("/search", json={"query": "q", "k": 99}).status_code == 422
    assert client.post("/search", json={"query": "q", "filters": {"origin": "x"}}).status_code == 422


def test_rest_bearer_token(fake):
    client = TestClient(rest_api.build(lambda: fake, token="s3cret"))
    assert client.get("/healthz").status_code == 200  # health stays open
    assert client.post("/search", json={"query": "q"}).status_code == 401
    ok = client.post("/search", json={"query": "q"}, headers={"Authorization": "Bearer s3cret"})
    assert ok.status_code == 200


def test_mcp_tools_match_rest_payload(fake):
    server = mcp_server.build(lambda: fake)
    rest = TestClient(rest_api.build(lambda: fake))

    async def run():
        async with Client(server) as c:
            assert {t.name for t in await c.list_tools()} == {"search_docs", "get_source"}
            res = await c.call_tool("search_docs", {"query": "q", "k": 3, "origin": "fork"})
            with pytest.raises(ToolError):
                await c.call_tool("get_source", {"repo": "o/r", "path": "nope.md"})
            return res.structured_content

    via_mcp = asyncio.run(run())
    via_rest = rest.post("/search", json={"query": "q", "k": 3,
                                          "filters": {"origin": "fork"}}).json()
    assert via_mcp == via_rest
