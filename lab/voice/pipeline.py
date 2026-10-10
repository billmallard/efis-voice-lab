"""The voice pipeline: transport in -> VAD -> STT -> text agent -> TTS -> transport out.

The LLM stage is the L2 `TextAgent` wrapped as a Pipecat processor, not Pipecat's own
LLM service, so a voice turn runs the identical prompt, model, MCP tool loop and speech
normalizer that L2 tests. That identity is what lets attribution (M5) compare an L3
failure with the same case in L2 and blame STT, the agent, or the voice path.

`TurnLog` records each turn's stage timestamps (monotonic seconds) alongside the
transcript and the agent turn, for L3 timing metrics and attribution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from lab.config import LabConfig


@dataclass
class Turn:
    t_user_started: float | None = None
    t_user_stopped: float | None = None
    t_transcript: float | None = None
    t_agent_done: float | None = None
    t_bot_started: float | None = None
    t_bot_stopped: float | None = None
    transcript: str = ""
    agent: Any = None                     # lab.agent.text_agent.AgentTurn

    def timing_ms(self) -> dict[str, float | None]:
        def gap(a: float | None, b: float | None) -> float | None:
            return round((b - a) * 1000, 1) if a is not None and b is not None else None

        return {
            "stt_ms": gap(self.t_user_stopped, self.t_transcript),
            "agent_ms": gap(self.t_transcript, self.t_agent_done),
            "tts_first_audio_ms": gap(self.t_agent_done, self.t_bot_started),
            # The latency budget's number: caller stops talking -> first audio back.
            "time_to_first_audio_ms": gap(self.t_user_stopped, self.t_bot_started),
            "bot_speech_ms": gap(self.t_bot_started, self.t_bot_stopped),
        }


@dataclass
class TurnLog:
    turns: list[Turn] = field(default_factory=list)

    def current(self) -> Turn:
        if not self.turns:
            self.turns.append(Turn())
        return self.turns[-1]


class AgentProcessor(FrameProcessor):
    """Final transcription in, the agent's spoken answer out (as a TTSSpeakFrame)."""

    def __init__(self, agent: Any, log: TurnLog, **kwargs: Any):
        super().__init__(**kwargs)
        self.agent = agent
        self.log = log

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        now = time.perf_counter()
        if isinstance(frame, VADUserStartedSpeakingFrame):
            turn = self.log.current()
            if turn.t_user_stopped is not None:     # a new utterance starts a new turn
                turn = Turn()
                self.log.turns.append(turn)
            turn.t_user_started = now
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self.log.current().t_user_stopped = now
        elif isinstance(frame, BotStartedSpeakingFrame):
            if self.log.current().t_bot_started is None:
                self.log.current().t_bot_started = now
        elif isinstance(frame, BotStoppedSpeakingFrame):
            self.log.current().t_bot_stopped = now

        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            turn = self.log.current()
            turn.t_transcript = now
            turn.transcript = frame.text.strip()
            result = await self.agent.ask(turn.transcript)
            turn.agent = result
            turn.t_agent_done = time.perf_counter()
            await self.push_frame(TTSSpeakFrame(result.spoken or result.answer))
            return
        await self.push_frame(frame, direction)


def build_services(cfg: LabConfig) -> tuple[Any, Any, Any]:
    """VAD, STT and TTS from config `models.stt` / `models.tts`."""
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.processors.audio.vad_processor import VADProcessor

    stt_ref, tts_ref = cfg.models["stt"], cfg.models["tts"]
    stt_extra, tts_extra = stt_ref.model_extra or {}, tts_ref.model_extra or {}
    if stt_ref.provider != "faster-whisper":
        raise ValueError(f"unsupported STT provider: {stt_ref.provider!r}")
    from pipecat.services.whisper.stt import WhisperSTTService

    stt = WhisperSTTService(
        settings=WhisperSTTService.Settings(model=stt_extra.get("size", "small.en")),
        device=stt_extra.get("device", "cpu"),
        compute_type=stt_extra.get("compute_type", "int8"))
    if tts_ref.provider == "kokoro":
        from pipecat.services.kokoro.tts import KokoroTTSService

        tts = KokoroTTSService(settings=KokoroTTSService.Settings(
            voice=tts_extra.get("voice") or "af_heart",
            speed=float(tts_extra.get("speed", 1.0))))
    else:
        raise ValueError(f"unsupported TTS provider: {tts_ref.provider!r}")
    return VADProcessor(vad_analyzer=SileroVADAnalyzer()), stt, tts


def build_pipeline(cfg: LabConfig, transport: Any, agent: Any, log: TurnLog) -> Any:
    from pipecat.pipeline.pipeline import Pipeline

    vad, stt, tts = build_services(cfg)
    return Pipeline([transport.input(), vad, stt, AgentProcessor(agent, log), tts,
                     transport.output()])
