"""Chat LLM providers for the agent, selected by config `models.agent_llm.provider`.

Messages use one provider-neutral shape, converted at the edge:
  {"role": "system" | "user", "content": str}
  {"role": "assistant", "content": str, "tool_calls": [ToolCallRequest, ...]}
  {"role": "tool", "tool_call_id": str, "name": str, "content": str}
Tools are MCP tool definitions: {"name", "description", "input_schema"}.
"""

from __future__ import annotations

import re
import time
import uuid
from typing import Any, Protocol

import httpx
from pydantic import BaseModel

from lab.config import LabConfig

_THINK = re.compile(r"<think>.*?</think>", re.DOTALL)


class ToolCallRequest(BaseModel):
    id: str
    name: str
    args: dict[str, Any]


class ChatResult(BaseModel):
    text: str
    tool_calls: list[ToolCallRequest] = []
    input_tokens: int = 0
    output_tokens: int = 0
    ms: float = 0.0
    stop_reason: str | None = None


class ChatLLM(Protocol):
    name: str

    async def chat(self, messages: list[dict[str, Any]],
                   tools: list[dict[str, Any]] | None = None) -> ChatResult: ...


class OllamaChat:
    """Ollama's native /api/chat, which supports tool calling and qwen3's `think` switch."""

    def __init__(self, base_url: str, name: str, temperature: float = 0.0,
                 think: bool | None = None, num_ctx: int | None = None,
                 max_tokens: int | None = None, timeout: float = 300.0):
        self.name = name
        self.base_url = base_url
        self.options = {k: v for k, v in (("temperature", temperature), ("num_ctx", num_ctx),
                                          ("num_predict", max_tokens)) if v is not None}
        self.think = think
        self._client = httpx.AsyncClient(base_url=base_url, timeout=timeout)

    async def ensure_model(self) -> None:
        if (await self._client.post("/api/show", json={"model": self.name})).status_code == 404:
            r = await self._client.post("/api/pull", json={"model": self.name, "stream": False},
                                        timeout=3600)
            r.raise_for_status()

    @staticmethod
    def _messages(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        out = []
        for m in messages:
            if m["role"] == "assistant" and m.get("tool_calls"):
                out.append({"role": "assistant", "content": m.get("content", ""),
                            "tool_calls": [{"function": {"name": c.name, "arguments": c.args}}
                                           for c in m["tool_calls"]]})
            elif m["role"] == "tool":
                out.append({"role": "tool", "content": m["content"], "tool_name": m["name"]})
            else:
                out.append({"role": m["role"], "content": m["content"]})
        return out

    async def chat(self, messages, tools=None) -> ChatResult:
        body: dict[str, Any] = {"model": self.name, "messages": self._messages(messages),
                                "stream": False, "options": self.options}
        if self.think is not None:
            body["think"] = self.think
        if tools:
            body["tools"] = [{"type": "function", "function": {
                "name": t["name"], "description": t["description"],
                "parameters": t["input_schema"]}} for t in tools]
        t0 = time.perf_counter()
        r = await self._client.post("/api/chat", json=body)
        r.raise_for_status()
        data = r.json()
        msg = data["message"]
        calls = [ToolCallRequest(id=c.get("id") or uuid.uuid4().hex[:12],
                                 name=c["function"]["name"],
                                 args=c["function"].get("arguments") or {})
                 for c in msg.get("tool_calls") or []]
        return ChatResult(text=_THINK.sub("", msg.get("content") or "").strip(), tool_calls=calls,
                          input_tokens=data.get("prompt_eval_count", 0),
                          output_tokens=data.get("eval_count", 0),
                          ms=(time.perf_counter() - t0) * 1000,
                          stop_reason=data.get("done_reason"))

    async def aclose(self) -> None:
        await self._client.aclose()


class AnthropicChat:
    """Hosted Claude as the agent LLM: the comparison path and the D2 demo, never the judge
    of its own answers."""

    def __init__(self, name: str, temperature: float = 0.0, max_tokens: int = 1024):
        from anthropic import AsyncAnthropic  # optional dependency: the `eval` extra

        self.name = name
        self.temperature = temperature
        self.max_tokens = max_tokens
        self._client = AsyncAnthropic()

    @staticmethod
    def _messages(messages: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]]]:
        system = "\n\n".join(m["content"] for m in messages if m["role"] == "system")
        out: list[dict[str, Any]] = []
        for m in messages:
            if m["role"] == "user":
                out.append({"role": "user", "content": m["content"]})
            elif m["role"] == "assistant":
                blocks: list[dict[str, Any]] = (
                    [{"type": "text", "text": m["content"]}] if m.get("content") else [])
                blocks += [{"type": "tool_use", "id": c.id, "name": c.name, "input": c.args}
                           for c in m.get("tool_calls") or []]
                out.append({"role": "assistant", "content": blocks})
            elif m["role"] == "tool":
                block = {"type": "tool_result", "tool_use_id": m["tool_call_id"],
                         "content": m["content"]}
                # All results for one assistant turn go back in a single user message.
                if out and out[-1]["role"] == "user" and isinstance(out[-1]["content"], list):
                    out[-1]["content"].append(block)
                else:
                    out.append({"role": "user", "content": [block]})
        return system, out

    async def chat(self, messages, tools=None) -> ChatResult:
        system, msgs = self._messages(messages)
        kwargs: dict[str, Any] = {}
        if tools:
            kwargs["tools"] = [{"name": t["name"], "description": t["description"],
                                "input_schema": t["input_schema"]} for t in tools]
        t0 = time.perf_counter()
        # SDK 1.x dropped the temperature kwarg; the API still takes it (see lab/judge.py).
        resp = await self._client.messages.create(
            model=self.name, max_tokens=self.max_tokens, system=system, messages=msgs,
            extra_body={"temperature": self.temperature}, **kwargs)
        text = "".join(b.text for b in resp.content if b.type == "text").strip()
        calls = [ToolCallRequest(id=b.id, name=b.name, args=dict(b.input))
                 for b in resp.content if b.type == "tool_use"]
        return ChatResult(text=text, tool_calls=calls, input_tokens=resp.usage.input_tokens,
                          output_tokens=resp.usage.output_tokens,
                          ms=(time.perf_counter() - t0) * 1000, stop_reason=resp.stop_reason)

    async def aclose(self) -> None:
        await self._client.close()


def from_config(cfg: LabConfig) -> ChatLLM:
    ref = cfg.models["agent_llm"]
    extra = ref.model_extra or {}
    if not ref.name:
        raise ValueError("models.agent_llm.name is not set")
    if ref.provider == "ollama":
        return OllamaChat(cfg.endpoints.agent, ref.name,
                          temperature=float(extra.get("temperature", 0.0)),
                          think=extra.get("think"), num_ctx=extra.get("num_ctx"),
                          max_tokens=extra.get("max_tokens"))
    if ref.provider == "anthropic":
        return AnthropicChat(ref.name, temperature=float(extra.get("temperature", 0.0)),
                             max_tokens=int(extra.get("max_tokens", 1024)))
    raise ValueError(f"unsupported agent provider: {ref.provider!r}")
