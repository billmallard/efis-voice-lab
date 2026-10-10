"""Smoke-test `lab serve-voice` the way the browser UI uses it: HTTP signaling to the
running server (POST /start, then the session's /api/offer), a WebRTC call from an
aiortc caller that speaks one synthesized question, and the bot's spoken reply recorded
and transcribed back.

    uv run lab serve-voice &          # in another shell
    uv run python scripts/check_serve_voice.py [--url http://localhost:7860]
"""

from __future__ import annotations

import argparse
import asyncio
import fractions
import sys
import time

import av
import httpx
import numpy as np
import soundfile as sf
from aiortc import MediaStreamTrack, RTCPeerConnection, RTCSessionDescription

RATE = 48000
FRAME = RATE // 50


class CallerTrack(MediaStreamTrack):
    kind = "audio"

    def __init__(self, pcm: np.ndarray, delay_s: float):
        super().__init__()
        self.pcm = np.concatenate([np.zeros(int(delay_s * RATE), np.int16), pcm])
        self.pos = 0
        self.t0: float | None = None
        self.speech_end: float | None = None

    async def recv(self):
        self.t0 = self.t0 or time.perf_counter()
        await asyncio.sleep(max(0.0, self.t0 + self.pos / RATE - time.perf_counter()))
        chunk = self.pcm[self.pos:self.pos + FRAME]
        if len(chunk) < FRAME:
            self.speech_end = self.speech_end or time.perf_counter()
            chunk = np.zeros(FRAME, np.int16)
        frame = av.AudioFrame.from_ndarray(chunk.reshape(1, -1), format="s16", layout="mono")
        frame.sample_rate, frame.pts = RATE, self.pos
        frame.time_base = fractions.Fraction(1, RATE)
        self.pos += FRAME
        return frame


async def call(url: str, question: str) -> int:
    from faster_whisper import WhisperModel

    from lab.voice.audio_synth import Synth, load_personas

    synth = Synth(RATE)
    pcm = synth.render(question, load_personas()["clean_m"])
    # The bot greets first; speak after the greeting has had time to play.
    track = CallerTrack(pcm, delay_s=8.0)
    pc = RTCPeerConnection()
    pc.addTrack(track)
    remote: asyncio.Future = asyncio.get_running_loop().create_future()
    pc.on("track", lambda t: remote.done() or t.kind != "audio" or remote.set_result(t))
    await pc.setLocalDescription(await pc.createOffer())
    async with httpx.AsyncClient(base_url=url, timeout=60) as http:
        start = (await http.post("/start", json={"transport": "webrtc"})).json()
        offer_path = (f"/sessions/{start['sessionId']}/api/offer" if start.get("sessionId")
                      else "/api/offer")
        answer = (await http.post(offer_path, json={
            "sdp": pc.localDescription.sdp, "type": pc.localDescription.type})).json()
    await pc.setRemoteDescription(RTCSessionDescription(sdp=answer["sdp"], type=answer["type"]))
    track_in = await asyncio.wait_for(remote, 30)
    resampler = av.AudioResampler(format="s16", layout="mono", rate=16000)
    got: list[tuple[float, np.ndarray]] = []
    deadline = time.perf_counter() + 120
    while time.perf_counter() < deadline:
        frame = await asyncio.wait_for(track_in.recv(), 15)
        now = time.perf_counter()
        for f in resampler.resample(frame):
            got.append((now, f.to_ndarray().reshape(-1)))
        if track.speech_end:
            loud = [t for t, a in got if t > track.speech_end and np.abs(a).max() > 300]
            if loud and now - loud[-1] > 3:
                break
    await pc.close()

    def speech(t0: float, t1: float) -> np.ndarray:
        parts = [a for t, a in got if t0 <= t <= t1]
        return np.concatenate(parts) if parts else np.zeros(0, np.int16)

    end = track.speech_end or time.perf_counter()
    loud = [t for t, a in got if t > end and np.abs(a).max() > 300]
    greeting, reply = speech(0, end), speech(end, float("inf"))
    sf.write("results/serve_voice_reply.wav", reply, 16000)
    whisper = WhisperModel("small.en", device="cpu", compute_type="int8")

    def text(a: np.ndarray) -> str:
        segs, _ = whisper.transcribe(a.astype(np.float32) / 32768)
        return " ".join(s.text.strip() for s in segs)

    print(f"greeting: {text(greeting)!r}")
    print(f"asked:    {question!r}")
    print(f"reply:    {text(reply)!r}")
    print(f"end of question to first reply audio: "
          f"{(loud[0] - end) * 1000:.0f} ms" if loud else "no reply audio")
    return 0 if loud else 1


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--url", default="http://localhost:7860")
    ap.add_argument("--question", default="What license is pie efis under?")
    args = ap.parse_args()
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    return asyncio.run(call(args.url, args.question))


if __name__ == "__main__":
    sys.exit(main())
