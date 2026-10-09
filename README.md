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

Default stack, all local and swappable through config: Python 3.11+ / `uv`, Docker Compose, Qdrant, Ollama
(embeddings + agent LLM), FastMCP, FastAPI, Pipecat, faster-whisper, Piper/Kokoro, and Asterisk.

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

Models and endpoints live in [config/lab.yaml](config/lab.yaml). `LAB_OLLAMA_URL` and `LAB_QDRANT_URL`
point the lab at services elsewhere, such as a home-lab server.

## Status

M0 (setup) and M1 (ingestion) are done. Milestone notes are in [docs/notes/](docs/notes/). The work is tracked as [milestones M0–M9](../../milestones) and on the
[project board](https://github.com/users/billmallard/projects/19). The full plan is in [docs/PLAN.md](docs/PLAN.md).

| Milestone | Scope |
|---|---|
| M0 ✓ | Setup and library spikes |
| M1 ✓ | Ingestion: 5 repos, 1,940 chunks |
| M2 | Retrieval service (MCP + REST) |
| M3 | Golden set + L1 retrieval suite |
| M4 | Text agent + L2 suite |
| M5 | Voice agent + L3 suite + attribution report |
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
