"""Talk to the voice agent in a browser: `uv run lab serve-voice`, then open
http://localhost:7860.

Pipecat's development runner serves its prebuilt browser UI and the SmallWebRTC
signaling; each browser session gets this module's `bot()`, which runs the same voice
pipeline as the L3 suite (lab/voice/pipeline.py) with its own text agent. The runner
finds `bot` on the `__main__` module, which is why `lab serve-voice` runs this file as
`__main__`.
"""

from __future__ import annotations

import sys

from loguru import logger

from lab import config


async def bot(runner_args) -> None:
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineTask
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

    from lab.agent.text_agent import open_agent
    from lab.voice.pipeline import TurnLog, build_pipeline, task_params

    cfg = config.load()
    transport = SmallWebRTCTransport(runner_args.webrtc_connection, TransportParams(
        audio_in_enabled=True, audio_out_enabled=True))
    log = TurnLog()
    async with open_agent(cfg) as agent:
        task = PipelineTask(build_pipeline(cfg, transport, agent, log),
                            params=task_params(cfg))

        @transport.event_handler("on_client_connected")
        async def greet(_transport, _client) -> None:
            from pipecat.frames.frames import TTSSpeakFrame

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
