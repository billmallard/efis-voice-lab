"""Talk to the voice agent in a browser: `uv run lab serve-voice`, then open
http://localhost:7860 (transport: SmallWebRTC).

Pipecat's development runner serves its prebuilt browser UI and the SmallWebRTC
signaling; each browser session gets this module's `bot()`, which runs the same voice
pipeline as the L3 suite (lab/voice/pipeline.py). The runner finds `bot` on the
`__main__` module, which is why `lab serve-voice` runs this file as `__main__`.
"""

from __future__ import annotations

import asyncio
import sys
from typing import Any

from loguru import logger

from lab import config

# One text agent (and its MCP server subprocess) for the life of the server: spawning
# it per session took ~7 s, all of it before the session could start listening.
_agent: Any = None
_agent_cm: Any = None
_agent_lock = asyncio.Lock()


async def shared_agent(cfg) -> Any:
    global _agent, _agent_cm
    async with _agent_lock:
        if _agent is None:
            from lab.agent.text_agent import open_agent

            _agent_cm = open_agent(cfg)
            _agent = await _agent_cm.__aenter__()
    return _agent


def trust_peer_connection(conn: Any) -> None:
    """Pipecat 1.12 workaround. `SmallWebRTCConnection.is_connected()` reports False
    once the browser's data channel has been quiet for 3 s. A bot that takes longer
    than that to start (loading Whisper, Kokoro, the agent) asks after the browser's
    last message has aged out, never sees the connection as up, and so never fires
    "connected": no audio is read and no greeting is sent. The peer connection itself is
    up; ask it."""
    conn.is_connected = lambda: (conn._connect_invoked
                                 and conn._pc.connectionState == "connected")


async def bot(runner_args) -> None:
    from pipecat.frames.frames import TTSSpeakFrame
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineTask
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

    from lab.voice.pipeline import TurnLog, build_pipeline, task_params

    cfg = config.load()
    trust_peer_connection(runner_args.webrtc_connection)
    transport = SmallWebRTCTransport(runner_args.webrtc_connection, TransportParams(
        audio_in_enabled=True, audio_out_enabled=True))
    agent = await shared_agent(cfg)
    log = TurnLog()
    task = PipelineTask(build_pipeline(cfg, transport, agent, log), params=task_params(cfg))

    @transport.event_handler("on_client_connected")
    async def greet(_transport, _client) -> None:
        logger.info("client connected; greeting")
        await task.queue_frames([TTSSpeakFrame(
            "Hi. Ask me anything about pyEfis, FIX Gateway, or navigation data.")])

    @transport.event_handler("on_client_disconnected")
    async def bye(_transport, _client) -> None:
        for t in log.turns:
            if t.kind == "answer":
                logger.info(f"turn: {t.transcript!r} -> {t.timing_ms()}")
        await task.cancel()

    await PipelineRunner(handle_sigint=False).run(task)


def main(host: str = "localhost", port: int = 7860) -> None:
    from pipecat.runner.run import main as runner_main

    sys.argv = [sys.argv[0], "--host", host, "--port", str(port), "-t", "webrtc"]
    runner_main()


if __name__ == "__main__":
    main(*sys.argv[1:2] or ["localhost"], *(int(p) for p in sys.argv[2:3]))
