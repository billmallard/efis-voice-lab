"""M5 decision-point spike: one synthesized question through the voice pipeline over
(a) Pipecat's websocket server transport and (b) SmallWebRTC with an aiortc loopback
caller. Both callers stream the WAV in real time, then keep sending silence (as a phone
line would) until the reply has finished, and record what comes back.

    uv run python scripts/spike_transport.py ws     --wav results/spike_q.wav
    uv run python scripts/spike_transport.py webrtc --wav results/spike_q.wav

Prints the server-side stage timings (TurnLog), the client-side end-of-speech to
first-audio gap, and writes the reply to results/spike_<transport>_reply.wav.
"""

from __future__ import annotations

import argparse
import asyncio
import fractions
import json
import sys
import time

import numpy as np
import soundfile as sf
import soxr

from lab import config
from lab.agent.text_agent import open_agent
from lab.voice.pipeline import TurnLog, build_pipeline

FRAME_MS = 20
SILENCE_DONE_S = 2.0          # reply finished after this long with no audio back
TIMEOUT_S = 120


def load_pcm(path: str, rate: int) -> np.ndarray:
    audio, sr = sf.read(path, dtype="float32", always_2d=False)
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    if sr != rate:
        audio = soxr.resample(audio, sr, rate)
    return (np.clip(audio, -1, 1) * 32767).astype(np.int16)


def report(name: str, log: TurnLog, t_speech_end: float, t_first_audio: float | None,
           reply: np.ndarray, rate: int) -> None:
    out = f"results/spike_{name}_reply.wav"
    sf.write(out, reply, rate)
    turn = log.turns[-1] if log.turns else None
    print(json.dumps({
        "transport": name,
        "transcript": turn.transcript if turn else None,
        "answer_spoken": turn.agent.spoken if turn and turn.agent else None,
        "server_timing_ms": turn.timing_ms() if turn else None,
        "client_end_of_speech_to_first_audio_ms":
            round((t_first_audio - t_speech_end) * 1000) if t_first_audio else None,
        "reply_seconds": round(len(reply) / rate, 2),
        "reply_wav": out,
    }, indent=2))


async def run_pipeline(cfg, transport, log, agent):
    from pipecat.pipeline.runner import PipelineRunner
    from pipecat.pipeline.task import PipelineParams, PipelineTask

    task = PipelineTask(build_pipeline(cfg, transport, agent, log),
                        params=PipelineParams(audio_in_sample_rate=16000,
                                              audio_out_sample_rate=24000))
    runner = PipelineRunner(handle_sigint=False)
    return task, asyncio.create_task(runner.run(task))


# ------------------------------------------------------------------- websocket

async def spike_ws(cfg, wav: str, port: int = 8790) -> None:
    import websockets
    from pipecat.frames.frames import OutputAudioRawFrame
    from pipecat.serializers.protobuf import ProtobufFrameSerializer
    from pipecat.transports.websocket.server import (
        WebsocketServerParams,
        WebsocketServerTransport,
    )

    rate_in, rate_out = 16000, 24000
    log = TurnLog()
    async with open_agent(cfg) as agent:
        transport = WebsocketServerTransport(
            params=WebsocketServerParams(audio_in_enabled=True, audio_out_enabled=True,
                                         add_wav_header=False,
                                         serializer=ProtobufFrameSerializer()),
            host="127.0.0.1", port=port)
        task, running = await run_pipeline(cfg, transport, log, agent)
        await asyncio.sleep(3)                                # let the server bind

        ser = ProtobufFrameSerializer()
        pcm = load_pcm(wav, rate_in)
        step = rate_in * FRAME_MS // 1000
        silence = np.zeros(step, dtype=np.int16)
        reply: list[np.ndarray] = []
        t_first_audio = None
        last_audio = None
        async with websockets.connect(f"ws://127.0.0.1:{port}", max_size=None) as ws:
            async def receive() -> None:
                nonlocal t_first_audio, last_audio
                async for msg in ws:
                    frame = await ser.deserialize(msg)
                    audio = getattr(frame, "audio", None)
                    if audio:
                        now = time.perf_counter()
                        t_first_audio = t_first_audio or now
                        last_audio = now
                        reply.append(np.frombuffer(audio, dtype=np.int16))

            receiver = asyncio.create_task(receive())

            async def send(chunk: np.ndarray) -> None:
                await ws.send(await ser.serialize(OutputAudioRawFrame(
                    audio=chunk.tobytes(), sample_rate=rate_in, num_channels=1)))
                await asyncio.sleep(FRAME_MS / 1000)

            for i in range(0, len(pcm), step):
                await send(pcm[i:i + step])
            t_speech_end = time.perf_counter()
            deadline = t_speech_end + TIMEOUT_S
            while time.perf_counter() < deadline:
                await send(silence)
                if last_audio and time.perf_counter() - last_audio > SILENCE_DONE_S:
                    break
            receiver.cancel()
        await task.cancel()
        running.cancel()
    report("ws", log, t_speech_end, t_first_audio,
           np.concatenate(reply) if reply else np.zeros(0, np.int16), rate_out)


# ---------------------------------------------------------------------- webrtc

async def spike_webrtc(cfg, wav: str) -> None:
    import av
    from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription
    from pipecat.transports.base_transport import TransportParams
    from pipecat.transports.smallwebrtc.request_handler import (
        SmallWebRTCRequest,
        SmallWebRTCRequestHandler,
    )
    from pipecat.transports.smallwebrtc.transport import SmallWebRTCTransport

    rate = 48000
    pcm = load_pcm(wav, rate)
    step = rate * FRAME_MS // 1000
    marks: dict[str, float] = {}

    class CallerTrack(MediaStreamTrack):
        """The question in real time, then silence, like an open phone line."""
        kind = "audio"

        def __init__(self) -> None:
            super().__init__()
            self.pos = 0
            self.start: float | None = None

        async def recv(self):
            self.start = self.start or time.perf_counter()
            due = self.start + self.pos / rate
            await asyncio.sleep(max(0.0, due - time.perf_counter()))
            chunk = pcm[self.pos:self.pos + step]
            if len(chunk) < step:
                marks.setdefault("speech_end", time.perf_counter())
                chunk = np.zeros(step, dtype=np.int16)
            frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16",
                                               layout="mono")
            frame.sample_rate, frame.pts = rate, self.pos
            frame.time_base = fractions.Fraction(1, rate)
            self.pos += step
            return frame

    log = TurnLog()
    reply: list[np.ndarray] = []
    async with open_agent(cfg) as agent:
        handler = SmallWebRTCRequestHandler()
        pc = RTCPeerConnection()
        pc.addTrack(CallerTrack())
        received = asyncio.get_running_loop().create_future()

        @pc.on("track")
        def on_track(track) -> None:
            if track.kind == "audio" and not received.done():
                received.set_result(track)

        started: dict[str, object] = {}

        async def on_connection(connection) -> None:
            transport = SmallWebRTCTransport(connection, TransportParams(
                audio_in_enabled=True, audio_out_enabled=True))
            started["task"], started["running"] = await run_pipeline(cfg, transport, log,
                                                                     agent)

        await pc.setLocalDescription(await pc.createOffer())
        answer = await handler.handle_web_request(
            SmallWebRTCRequest(sdp=pc.localDescription.sdp, type=pc.localDescription.type),
            on_connection)
        await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"],
                                                            type=answer["type"]))
        track = await asyncio.wait_for(received, 30)
        deadline = time.perf_counter() + TIMEOUT_S
        last_voice = None
        resampler = av.AudioResampler(format="s16", layout="mono", rate=24000)
        while time.perf_counter() < deadline:
            frame = await asyncio.wait_for(track.recv(), 10)
            for f in resampler.resample(frame):
                data = f.to_ndarray().reshape(-1)
                if np.abs(data).max(initial=0) > 300:        # remote sends silence too
                    now = time.perf_counter()
                    marks.setdefault("first_audio", now)
                    last_voice = now
                if "first_audio" in marks:
                    reply.append(data)
            if last_voice and time.perf_counter() - last_voice > SILENCE_DONE_S:
                break
        await pc.close()
        await handler.close()
        if "task" in started:
            await started["task"].cancel()
            started["running"].cancel()
    report("webrtc", log, marks.get("speech_end", time.perf_counter()), marks.get("first_audio"),
           np.concatenate(reply) if reply else np.zeros(0, np.int16), 24000)


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("transport", choices=["ws", "webrtc"])
    ap.add_argument("--wav", default="results/spike_q.wav")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg = config.load()
    asyncio.run(spike_ws(cfg, args.wav) if args.transport == "ws"
                else spike_webrtc(cfg, args.wav))
    return 0


if __name__ == "__main__":
    sys.exit(main())
