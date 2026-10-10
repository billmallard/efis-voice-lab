"""The voice pipeline:

    transport in -> Silero VAD -> user turns -> STT -> text agent -> TTS -> transport out

The LLM stage is the L2 `TextAgent` wrapped as a Pipecat processor, not Pipecat's own
LLM service, so a voice turn runs the identical prompt, model, MCP tool loop and speech
normalizer that L2 tests. That identity is what lets attribution compare an L3 failure
with the same case in L2 and blame STT, the agent, or the voice path.

The user-turn stage (Pipecat's UserTurnProcessor) owns turn-taking: caller speech starts
a turn and, with `voice.interruptions`, cuts the bot off; a pause of
`voice.user_speech_timeout_s` ends it; `voice.idle_timeout_s` of caller silence makes
the bot re-prompt.

`TurnLog` records each turn's stage timestamps (`perf_counter` seconds), the transcript,
the agent turn, and whether the bot was interrupted, for L3 metrics and attribution.
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import Any

from pipecat.frames.frames import (
    BotStartedSpeakingFrame,
    BotStoppedSpeakingFrame,
    Frame,
    InterruptionFrame,
    TranscriptionFrame,
    TTSSpeakFrame,
    VADUserStartedSpeakingFrame,
    VADUserStoppedSpeakingFrame,
)
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor

from lab.config import LabConfig


@dataclass
class Turn:
    kind: str = "answer"                  # answer | idle_prompt
    t_user_started: float | None = None
    t_user_stopped: float | None = None
    t_transcript: float | None = None
    t_agent_done: float | None = None
    t_bot_started: float | None = None
    t_bot_stopped: float | None = None
    t_interrupted: float | None = None    # caller barged in while this turn was speaking
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
            "interrupt_to_silence_ms": gap(self.t_interrupted, self.t_bot_stopped),
        }


@dataclass
class TurnLog:
    turns: list[Turn] = field(default_factory=list)

    def current(self) -> Turn:
        if not self.turns:
            self.turns.append(Turn())
        return self.turns[-1]


class AgentProcessor(FrameProcessor):
    """Final transcription in, the agent's spoken answer out (as a TTSSpeakFrame).

    It also sees the turn-taking frames on their way past, which is where `TurnLog`'s
    timestamps come from."""

    def __init__(self, agent: Any, log: TurnLog, **kwargs: Any):
        super().__init__(**kwargs)
        self.agent = agent
        self.log = log
        self._awaiting_audio: Turn | None = None   # spoke a TTSSpeakFrame, no audio yet
        self._speaking: Turn | None = None         # the turn whose audio is playing

    async def speak(self, text: str, turn: Turn) -> None:
        self._awaiting_audio = turn
        await self.push_frame(TTSSpeakFrame(text))

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        now = time.perf_counter()
        if isinstance(frame, VADUserStartedSpeakingFrame):
            turn = self.log.current()
            if turn.t_user_stopped is not None or turn.kind != "answer":
                turn = Turn()                       # a new utterance starts a new turn
                self.log.turns.append(turn)
            turn.t_user_started = now
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self.log.current().t_user_stopped = now
        elif isinstance(frame, InterruptionFrame):
            if self._speaking is not None and self._speaking.t_interrupted is None:
                self._speaking.t_interrupted = now
        elif isinstance(frame, BotStartedSpeakingFrame):
            if self._awaiting_audio is not None:
                self._speaking, self._awaiting_audio = self._awaiting_audio, None
                self._speaking.t_bot_started = now
        elif isinstance(frame, BotStoppedSpeakingFrame):
            if self._speaking is not None:
                self._speaking.t_bot_stopped = now
                self._speaking = None

        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            turn = self.log.current()
            turn.t_transcript = now
            turn.transcript = frame.text.strip()
            result = await self.agent.ask(turn.transcript)
            turn.agent = result
            turn.t_agent_done = time.perf_counter()
            await self.speak(result.spoken or result.answer, turn)
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
        settings=WhisperSTTService.Settings(model=stt_extra.get("size", "small.en"),
                                            hotwords=stt_extra.get("hotwords"),
                                            initial_prompt=stt_extra.get("initial_prompt")),
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


def build_turn_processor(cfg: LabConfig) -> Any:
    from pipecat.turns.user_start.vad_user_turn_start_strategy import (
        VADUserTurnStartStrategy,
    )
    from pipecat.turns.user_stop.speech_timeout_user_turn_stop_strategy import (
        SpeechTimeoutUserTurnStopStrategy,
    )
    from pipecat.turns.user_turn_processor import UserTurnProcessor
    from pipecat.turns.user_turn_strategies import UserTurnStrategies

    v = cfg.voice
    return UserTurnProcessor(
        user_turn_strategies=UserTurnStrategies(
            start=[VADUserTurnStartStrategy(enable_interruptions=v.interruptions)],
            stop=[SpeechTimeoutUserTurnStopStrategy(
                user_speech_timeout=v.user_speech_timeout_s)]),
        user_idle_timeout=v.idle_timeout_s)


def build_pipeline(cfg: LabConfig, transport: Any, agent: Any, log: TurnLog) -> Any:
    from pipecat.pipeline.pipeline import Pipeline

    vad, stt, tts = build_services(cfg)
    turns = build_turn_processor(cfg)
    agent_proc = AgentProcessor(agent, log)

    @turns.event_handler("on_user_turn_idle")
    async def on_idle(_processor) -> None:
        turn = Turn(kind="idle_prompt", t_agent_done=time.perf_counter())
        log.turns.append(turn)
        await agent_proc.speak(cfg.voice.idle_prompt, turn)

    return Pipeline([transport.input(), vad, turns, stt, agent_proc, tts,
                     transport.output()])


def task_params(cfg: LabConfig) -> Any:
    from pipecat.pipeline.task import PipelineParams

    return PipelineParams(audio_in_sample_rate=cfg.voice.sample_rate_in,
                          audio_out_sample_rate=cfg.voice.sample_rate_out)
