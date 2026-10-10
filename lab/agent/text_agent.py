"""The text agent: the voice agent's prompt, LLM and MCP tool loop, without audio (L2).

Tools are discovered from the MCP server (`lab serve-mcp`), the same one the Pipecat agent
will use in M5, so L2 exercises the real tool interface rather than a Python shortcut.
Config `agent.tools` picks which MCP tools the LLM sees, and `agent.hidden_args` removes
arguments the harness controls (k, repo filters) from their schemas.

`ask()` returns an `AgentTurn`: the answer plus everything attribution needs later --
each tool call with its arguments and ranked hits, the retrieved context, per-stage
timing and token counts.
"""

from __future__ import annotations

import copy
import sys
import time
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from pydantic import BaseModel

from lab.agent import llm as llm_mod
from lab.config import ROOT, LabConfig

HIT_KEYS = ("id", "repo", "origin", "path", "heading", "score")


class ToolCallRecord(BaseModel):
    name: str
    args: dict[str, Any]
    ms: float
    error: str | None = None
    hits: list[dict[str, Any]] = []   # search_docs: ranked hits, without text


class AgentTurn(BaseModel):
    question: str
    answer: str
    model: str
    stop: str                          # answered | tool_budget | empty
    rounds: int
    tool_calls: list[ToolCallRecord]
    contexts: list[str]                # every distinct chunk the model was shown, in order
    retrieved: list[dict[str, Any]]    # the same chunks' metadata (repo, path, heading, ...)
    timing: dict[str, float]           # total_ms, llm_ms, tool_ms, first_llm_ms
    usage: dict[str, int]              # agent LLM tokens, summed over rounds

    @property
    def searched(self) -> bool:
        return any(c.name == "search_docs" and not c.error for c in self.tool_calls)


def _render_hits(hits: list[dict[str, Any]]) -> str:
    if not hits:
        return "No matching documentation found."
    parts = []
    for i, h in enumerate(hits, 1):
        parts.append(f"[{i}] {h['repo']} ({h['origin']}) {h['path']} -- {h['heading']}\n"
                     f"{h['text'].strip()}")
    return "\n\n".join(parts)


class TextAgent:
    def __init__(self, cfg: LabConfig, mcp_client: Any, chat: llm_mod.ChatLLM | None = None):
        self.cfg = cfg
        self.mcp = mcp_client                  # a connected fastmcp.Client
        self.llm = chat or llm_mod.from_config(cfg)
        self.system_prompt = cfg.agent.prompt_text()
        self._tools: list[dict[str, Any]] | None = None

    async def tools(self) -> list[dict[str, Any]]:
        """MCP tool definitions offered to the LLM, with hidden arguments removed."""
        if self._tools is None:
            hidden = set(self.cfg.agent.hidden_args)
            self._tools = []
            for t in await self.mcp.list_tools():
                if t.name not in self.cfg.agent.tools:
                    continue
                schema = copy.deepcopy(t.input_schema)
                for arg in hidden:
                    schema.get("properties", {}).pop(arg, None)
                if "required" in schema:
                    schema["required"] = [r for r in schema["required"] if r not in hidden]
                self._tools.append({"name": t.name, "description": t.description or "",
                                    "input_schema": schema})
        return self._tools

    async def _call_tool(self, name: str, args: dict[str, Any]) -> tuple[ToolCallRecord, str,
                                                                         list[dict[str, Any]]]:
        offered = {t["name"] for t in await self.tools()}
        hidden = set(self.cfg.agent.hidden_args)
        args = {k: v for k, v in args.items() if k not in hidden and v is not None}
        if name not in offered:
            return (ToolCallRecord(name=name, args=args, ms=0.0, error="unknown tool"),
                    f"Error: there is no tool named {name}.", [])
        call_args = dict(args)
        if name == "search_docs" and "k" in hidden:
            call_args["k"] = self.cfg.agent.search_k
        t0 = time.perf_counter()
        res = await self.mcp.call_tool(name, call_args, raise_on_error=False)
        ms = (time.perf_counter() - t0) * 1000
        if res.is_error:
            msg = " ".join(getattr(c, "text", "") for c in res.content) or "tool error"
            return ToolCallRecord(name=name, args=args, ms=ms, error=msg), f"Error: {msg}", []
        data = res.structured_content or {}
        if name == "search_docs":
            hits = data.get("hits", [])
            rec = ToolCallRecord(name=name, args=args, ms=ms,
                                 hits=[{k: h[k] for k in HIT_KEYS} for h in hits])
            return rec, _render_hits(hits), hits
        sections = data.get("sections", [])
        text = "\n\n".join(f"{s['heading']}\n{s['text']}" for s in sections) or str(data)
        return ToolCallRecord(name=name, args=args, ms=ms), text, []

    async def ask(self, question: str) -> AgentTurn:
        messages: list[dict[str, Any]] = [{"role": "system", "content": self.system_prompt},
                                          {"role": "user", "content": question}]
        tools = await self.tools()
        records: list[ToolCallRecord] = []
        seen: dict[str, dict[str, Any]] = {}
        llm_ms = tool_ms = 0.0
        first_llm_ms: float | None = None
        usage = {"input_tokens": 0, "output_tokens": 0}
        t0 = time.perf_counter()
        max_rounds = self.cfg.agent.max_tool_rounds
        answer, stop, rounds = "", "empty", 0
        for rounds in range(1, max_rounds + 2):
            allow_tools = rounds <= max_rounds
            res = await self.llm.chat(messages, tools if allow_tools else None)
            llm_ms += res.ms
            first_llm_ms = res.ms if first_llm_ms is None else first_llm_ms
            usage["input_tokens"] += res.input_tokens
            usage["output_tokens"] += res.output_tokens
            if res.tool_calls and allow_tools:
                messages.append({"role": "assistant", "content": res.text,
                                 "tool_calls": res.tool_calls})
                for call in res.tool_calls:
                    rec, text, hits = await self._call_tool(call.name, call.args)
                    tool_ms += rec.ms
                    records.append(rec)
                    for h in hits:
                        seen.setdefault(h["id"], h)
                    messages.append({"role": "tool", "tool_call_id": call.id,
                                     "name": call.name, "content": text})
                continue
            answer = res.text
            # An empty reply on the forced no-tools round means the model spent its budget
            # searching and never settled on an answer.
            stop = "answered" if answer else ("tool_budget" if not allow_tools else "empty")
            break
        return AgentTurn(
            question=question, answer=answer, model=self.llm.name, stop=stop, rounds=rounds,
            tool_calls=records,
            contexts=[h["text"] for h in seen.values()],
            retrieved=[{k: h[k] for k in ("repo", "origin", "path", "heading")}
                       for h in seen.values()],
            timing={"total_ms": round((time.perf_counter() - t0) * 1000, 1),
                    "llm_ms": round(llm_ms, 1), "tool_ms": round(tool_ms, 1),
                    "first_llm_ms": round(first_llm_ms or 0.0, 1)},
            usage=usage,
        )


@asynccontextmanager
async def open_agent(cfg: LabConfig, log_file: Path | None = None):
    """A TextAgent connected to `lab serve-mcp` over stdio, as the voice agent will be."""
    from fastmcp import Client
    from fastmcp.client.transports import StdioTransport

    log = log_file or ROOT / "results" / "mcp-server.log"
    log.parent.mkdir(parents=True, exist_ok=True)
    client = Client(StdioTransport(sys.executable, ["-m", "lab.cli", "serve-mcp"],
                                   cwd=str(ROOT), log_file=log))
    async with client:
        agent = TextAgent(cfg, client)
        try:
            yield agent
        finally:
            await agent.llm.aclose()
