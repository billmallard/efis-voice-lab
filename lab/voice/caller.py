"""The voice server (in-process, websocket transport) and a scripted test caller.

`voice_session()` starts the voice pipeline on Pipecat's websocket server transport and
yields its URL and `TurnLog`. `WsCaller` is an open phone line: it sends 20 ms frames in
real time, silence whenever it isn't speaking, and timestamps every audio frame that
comes back, so tests can script speech, pauses and barge-in and measure the bot's
timing from the caller's side.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Self

import numpy as np

from lab.config import LabConfig
from lab.voice.pipeline import TurnLog, build_pipeline, task_params

FRAME_MS = 20
SPEECH_RMS = 300            # int16 RMS above this counts as bot speech


@asynccontextmanager
async def voice_session(cfg: LabConfig, port: int, agent=None) -> AsyncIterator[tuple[str,
                                                                                     TurnLog]]:
    """The voice pipeline served over websocket on 127.0.0.1:`port`. Pass an open
    `TextAgent` to share one, or let the session open its own."""
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineTask
    from pipecat.serializers.protobuf import ProtobufFrameSerializer
    from pipecat.transports.websocket.server import (
        WebsocketServerParams,
        WebsocketServerTransport,
    )

    from lab.agent.text_agent import open_agent

    async def run(agent) -> AsyncIterator[tuple[str, TurnLog]]:
        log = TurnLog()
        transport = WebsocketServerTransport(
            params=WebsocketServerParams(audio_in_enabled=True, audio_out_enabled=True,
                                         add_wav_header=False,
                                         serializer=ProtobufFrameSerializer()),
            host="127.0.0.1", port=port)
        task = PipelineTask(build_pipeline(cfg, transport, agent, log),
                            params=task_params(cfg))
        running = asyncio.create_task(PipelineRunner(handle_sigint=False).run(task))
        await asyncio.sleep(2)                      # let the server bind
        try:
            yield f"ws://127.0.0.1:{port}", log
        finally:
            await task.cancel()
            running.cancel()

    if agent is not None:
        async for item in run(agent):
            yield item
        return
    async with open_agent(cfg) as own:
        async for item in run(own):
            yield item


@dataclass
class Received:
    frames: list[tuple[float, np.ndarray]] = field(default_factory=list)

    def speech_after(self, t: float) -> list[tuple[float, np.ndarray]]:
        return [(ts, a) for ts, a in self.frames if ts >= t
                and np.sqrt(np.mean(a.astype(np.float32) ** 2)) > SPEECH_RMS]

    def first_speech_after(self, t: float) -> float | None:
        s = self.speech_after(t)
        return s[0][0] if s else None

    def last_speech_after(self, t: float) -> float | None:
        s = self.speech_after(t)
        return s[-1][0] if s else None

    def audio_between(self, t0: float, t1: float) -> np.ndarray:
        parts = [a for ts, a in self.frames if t0 <= ts <= t1]
        return np.concatenate(parts) if parts else np.zeros(0, np.int16)


class WsCaller:
    def __init__(self, url: str, rate_in: int = 16000, rate_out: int = 24000):
        self.url, self.rate_in, self.rate_out = url, rate_in, rate_out
        self.step = rate_in * FRAME_MS // 1000
        self.received = Received()
        self._queue: asyncio.Queue[tuple[np.ndarray, asyncio.Future]] = asyncio.Queue()
        self._tasks: list[asyncio.Task] = []

    async def __aenter__(self) -> Self:
        import websockets
        from pipecat.serializers.protobuf import ProtobufFrameSerializer

        self._ser = ProtobufFrameSerializer()
        self._ws = await websockets.connect(self.url, max_size=None)
        self._tasks = [asyncio.create_task(self._send_loop()),
                       asyncio.create_task(self._recv_loop())]
        return self

    async def __aexit__(self, *exc) -> None:
        for t in self._tasks:
            t.cancel()
        await self._ws.close()

    async def _send_loop(self) -> None:
        from pipecat.frames.frames import OutputAudioRawFrame

        silence = np.zeros(self.step, dtype=np.int16)
        current: np.ndarray | None = None
        done: asyncio.Future | None = None
        pos = 0
        t_next = time.perf_counter()
        while True:
            if current is None and not self._queue.empty():
                current, done = self._queue.get_nowait()
                pos = 0
                if len(current) == 0:
                    done.set_result(time.perf_counter())
                    current = None
            if current is not None:
                chunk = current[pos:pos + self.step]
                if len(chunk) < self.step:
                    chunk = np.pad(chunk, (0, self.step - len(chunk)))
                pos += self.step
                if pos >= len(current):
                    done.set_result(time.perf_counter() + FRAME_MS / 1000)
                    current = None
            else:
                chunk = silence
            await self._ws.send(await self._ser.serialize(OutputAudioRawFrame(
                audio=chunk.tobytes(), sample_rate=self.rate_in, num_channels=1)))
            t_next += FRAME_MS / 1000
            await asyncio.sleep(max(0.0, t_next - time.perf_counter()))

    async def _recv_loop(self) -> None:
        async for msg in self._ws:
            frame = await self._ser.deserialize(msg)
            audio = getattr(frame, "audio", None)
            if audio:
                self.received.frames.append((time.perf_counter(),
                                             np.frombuffer(audio, dtype=np.int16)))

    async def say(self, pcm: np.ndarray) -> tuple[float, float]:
        """Speak `pcm` (int16 at rate_in) in real time. Returns (start, end) times."""
        done = asyncio.get_running_loop().create_future()
        start = time.perf_counter()
        await self._queue.put((pcm, done))
        end = await done
        return start, end

    async def wait_for_reply(self, after: float, timeout: float = 90.0,
                             quiet: float = 2.0) -> tuple[float | None, float | None]:
        """Wait for the bot to speak after `after` and then go quiet for `quiet` seconds.
        Returns (first, last) speech times, or (None, None) on timeout."""
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            last = self.received.last_speech_after(after)
            if last is not None and time.perf_counter() - last > quiet:
                return self.received.first_speech_after(after), last
            await asyncio.sleep(0.1)
        return self.received.first_speech_after(after), self.received.last_speech_after(after)

    async def wait_for_speech(self, after: float, timeout: float) -> float | None:
        deadline = time.perf_counter() + timeout
        while time.perf_counter() < deadline:
            t = self.received.first_speech_after(after)
            if t is not None:
                return t
            await asyncio.sleep(0.05)
        return None
