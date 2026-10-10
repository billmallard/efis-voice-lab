# EFIS Voice Lab

[![ci](https://github.com/billmallard/efis-voice-lab/actions/workflows/ci.yml/badge.svg)](https://github.com/billmallard/efis-voice-lab/actions/workflows/ci.yml)

A local, free-to-run lab for **building and testing a voice AI agent backed by RAG**.

The agent answers spoken questions, including over a phone line, about the open-source
[pyEfis](https://github.com/billmallard/pyEfis) experimental-aircraft EFIS and the related
MakerPlane docs. The agent is only the test subject. The point of the project is the test harness:
it evaluates every layer of the voice pipeline, and when an answer is wrong it **reports which layer
caused the failure** (STT, retrieval, generation, policy, latency, turn-taking, or telephony).

## What it demonstrates

- **Layered evaluation**: L1 retrieval, L2 text agent, L3 voice (audio in/out), and L4 telephony (SIP over G.711).
- **RAG metrics** with [Ragas]: context precision/recall, faithfulness, and answer relevancy.
- **LLM-as-judge** rubrics, including a *speakability* judge: answers must sound right read aloud, with no symbols or file paths spoken.
- **Simulated callers**: TTS-synthesized questions with noise, fast speech, accents, barge-in, and dead air.
- **Failure attribution**: each failed case gets one primary root-cause tag.
- **Regression in CI**: re-index when upstream docs change and catch golden-set drift.
- Tests written in [Robot Framework] 7 on a custom Python keyword library.

## Architecture

```
 pyEfis + MakerPlane docs ─▶ Ingestion ─▶ Qdrant ◀─ Retrieval core ─┬─ MCP server ─▶ Pipecat voice agent ◀─ Asterisk ◀─ caller bot / softphone
                                                                     └─ REST API   ─▶ hosted agents (optional)
 Test harness (Robot Framework + Ragas): L1 retrieval · L2 agent · L3 voice · L4 telephony · attribution report
```

Default stack, swappable through config: Python 3.11+ / `uv`, Docker Compose, Qdrant, Ollama
(embeddings + the agent under test), FastMCP, FastAPI, Pipecat, faster-whisper, Piper/Kokoro, and Asterisk.
The judge is hosted: Claude Haiku 4.5, a different model family from the local agent it grades.
The test report and the live demo are hosted on Cloudflare (Pages, Tunnel, Access).

## Quickstart

Requires Docker and [uv](https://docs.astral.sh/uv/).

```sh
uv sync --all-extras
docker compose up -d --wait          # Qdrant + Ollama (add -f docker-compose.gpu.yml for NVIDIA)
uv run python scripts/smoke.py       # embed, store, and retrieve by paraphrase; pulls the model on first run
uv run python scripts/check_libs.py  # versions + API surface of ragas / fastmcp / pipecat / qdrant
```

Build and query the docs index (sources in [config/sources.yaml](config/sources.yaml)):

```sh
uv run lab ingest                     # clone, chunk, embed, upsert; re-runs skip unchanged sources
uv run lab stats                      # chunks per source and commit
uv run lab query "Do I need a GPU to run pyEfis?" --origin fork
uv run python scripts/check_index.py  # metadata completeness, exclusions, probe queries
```

Serve the index. Both interfaces share one search core:

```sh
uv run lab serve-api                       # REST: POST /search, GET /source  (http://127.0.0.1:8765/docs)
uv run lab serve-mcp                       # MCP over stdio: search_docs, get_source
uv run python scripts/check_retrieval.py   # REST vs MCP give identical results; p95 latency
```

Opening this repo in Claude Code picks up the `efis-docs` MCP server from [.mcp.json](.mcp.json).

Run the L1 retrieval suite on the [golden set](golden/). Put `ANTHROPIC_API_KEY` in `.env`
(see [.env.example](.env.example)) to turn on the Ragas judge:

```sh
uv run robot --outputdir results/L1/robot tests/L1_retrieval   # one test per golden case
uv run robot -v L1_IDS:HW-003,VER-002 tests/L1_retrieval      # a subset
```

Scores go to `results/L1/latest.json`; the reference run is
[baselines/L1/2026-10-09.json](baselines/L1/2026-10-09.json).

Models and endpoints live in [config/lab.yaml](config/lab.yaml). `LAB_OLLAMA_URL` and `LAB_QDRANT_URL`
point the lab at services elsewhere, such as a home-lab server.

## Status

M0–M3 are done: setup, ingestion, the retrieval service, and the golden set with its L1 suite. Milestone notes are in [docs/notes/](docs/notes/). The work is tracked as [milestones M0–M9](../../milestones) and on the
[project board](https://github.com/users/billmallard/projects/19). The full plan is in [docs/PLAN.md](docs/PLAN.md).

| Milestone | Scope |
|---|---|
| M0 ✓ | Setup and library spikes |
| M1 ✓ | Ingestion: 5 repos, 1,940 chunks |
| M2 ✓ | Retrieval service: MCP + REST, p95 98 ms |
| M3 ✓ | Golden set (35 cases, 9 doc defects) + L1 suite: hit rate 0.87 |
| M4 | Text agent + L2 suite |
| M5 | Voice agent + L3 suite + attribution report |
| D1 | Published test report on Cloudflare Pages (after M5) |
| D2 | Hosted live demo: Pages + Tunnel + Access (after M5) |
| M6 | Telephony + L4 suite |
| M7 | CI and drift detection |
| M8 | Cross-platform comparison (optional) |
| M9 | Write-up |

## Safety scope

The agent never makes airworthiness, certification, or in-flight operational judgments. Dedicated
out-of-scope test cases enforce this.

## License

Apache-2.0. The indexed documentation keeps its own upstream licenses and is not redistributed here.

[Ragas]: https://github.com/explodinggradients/ragas
[Robot Framework]: https://robotframework.org
