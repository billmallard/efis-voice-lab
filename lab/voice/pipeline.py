"""The voice pipeline:

    transport in -> Silero VAD -> STT -> user turns -> text agent -> TTS -> transport out

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

import asyncio
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
    UserStoppedSpeakingFrame,
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
    fragments: list[str] = field(default_factory=list)   # STT segments within the turn
    agent: Any = None                     # lab.agent.text_agent.AgentTurn

    def timing_ms(self) -> dict[str, float | None]:
        def gap(a: float | None, b: float | None) -> float | None:
            return round((b - a) * 1000, 1) if a is not None and b is not None else None

        return {
            # Caller's last word -> turn handed to the agent: STT plus the end-of-turn
            # wait (voice.user_speech_timeout_s).
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
    """The caller's whole turn in, the agent's spoken answer out (as a TTSSpeakFrame).

    It collects STT segments until the user-turn processor ends the turn, then answers
    once. It also sees the turn-taking frames on their way past, which is where
    `TurnLog`'s timestamps come from."""

    TRANSCRIPT_GRACE_S = 0.15
    # Whisper runs behind real time on CPU (~2 s a segment). The end-of-turn strategy
    # needs only one transcript, so a two-sentence question could be answered from its
    # first sentence. Wait until every VAD segment has been transcribed, up to this
    # long: a segment of pure noise yields no transcript at all.
    SEGMENT_WAIT_MAX_S = 10.0

    def __init__(self, agent: Any, log: TurnLog, **kwargs: Any):
        super().__init__(**kwargs)
        self.agent = agent
        self.log = log
        self._awaiting_audio: Turn | None = None   # spoke a TTSSpeakFrame, no audio yet
        self._speaking: Turn | None = None         # the turn whose audio is playing
        self._fragments: list[str] = []            # STT segments of the turn in progress
        self._t_stop: float | None = None           # end of the caller's turn, unanswered
        self._segments = 0                          # VAD segments since the last answer
        self._transcribed = 0                       # ... and transcripts received for them
        self._user_speaking = False                 # VAD: the caller is mid-utterance
        self._handed_off = False                    # turn given to the agent, no new speech yet
        self.late_transcripts: list[str] = []       # arrived after their turn was answered
        self._answer_task: asyncio.Task | None = None

    async def speak(self, text: str, turn: Turn) -> None:
        self._awaiting_audio = turn
        await self.push_frame(TTSSpeakFrame(text))

    async def process_frame(self, frame: Frame, direction: FrameDirection) -> None:
        await super().process_frame(frame, direction)
        now = time.perf_counter()
        if isinstance(frame, VADUserStartedSpeakingFrame):
            turn = self.log.current()
            # A pause inside one question is several VAD segments of the same turn; only
            # a turn already handed to the agent (or an idle prompt) starts a new one.
            if turn.t_transcript is not None or turn.kind != "answer":
                turn = Turn()
                self.log.turns.append(turn)
            if turn.t_user_started is None:
                turn.t_user_started = now
            self._user_speaking = True
            self._handed_off = False
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self.log.current().t_user_stopped = now
            self._segments += 1
            self._user_speaking = False
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

        if isinstance(frame, InterruptionFrame) and self._answer_task is not None:
            # The caller started talking again before the answer was ready: drop it and
            # keep collecting (their words go back into the turn in progress).
            self._answer_task.cancel()
            self._answer_task = None

        if isinstance(frame, TranscriptionFrame):
            # Whisper transcribes each VAD segment; collect them until the turn ends.
            self._transcribed += 1
            if self._handed_off:
                # A segment of a turn that was already answered; merging it into the
                # next question would put words in the caller's mouth.
                if frame.text.strip():
                    self.late_transcripts.append(frame.text.strip())
                return
            if frame.text.strip():
                self._fragments.append(frame.text.strip())
                if self._t_stop is not None and self._answer_task is None:
                    self._answer_task = asyncio.create_task(self._answer())
            return
        if isinstance(frame, UserStoppedSpeakingFrame):
            # The user-turn processor says the caller has finished. It can announce that
            # just before forwarding the transcript that decided it, so the answer waits
            # a moment for that last fragment instead of trusting frame order.
            await self.push_frame(frame, direction)
            self._t_stop = now
            if self._answer_task is None:
                self._answer_task = asyncio.create_task(self._answer())
            return
        await self.push_frame(frame, direction)

    async def _answer(self) -> None:
        await asyncio.sleep(self.TRANSCRIPT_GRACE_S)
        deadline = time.perf_counter() + self.SEGMENT_WAIT_MAX_S
        # The end-of-turn strategy can fire on a transcript while the caller has already
        # resumed; never answer mid-utterance or before every segment is transcribed.
        while ((self._user_speaking or self._transcribed < self._segments)
               and time.perf_counter() < deadline):
            await asyncio.sleep(0.05)
        if not self._fragments:                 # a turn end with no words yet: keep waiting
            self._answer_task = None
            return
        fragments, self._fragments = list(self._fragments), []
        self._segments = self._transcribed = 0
        self._handed_off = True
        turn = self.log.current()
        # Handed to the agent now: stt_ms then covers the whole wait, segments included.
        turn.t_transcript, self._t_stop = time.perf_counter(), None
        turn.fragments, turn.transcript = fragments, " ".join(fragments)
        try:
            result = await self.agent.ask(turn.transcript)
        except asyncio.CancelledError:
            self._fragments[:0] = fragments     # the turn goes on; its words are kept
            self._handed_off = False
            turn.t_transcript, turn.fragments, turn.transcript = None, [], ""
            raise
        turn.agent = result
        turn.t_agent_done = time.perf_counter()
        self._answer_task = None
        await self.speak(result.spoken or result.answer, turn)


def _eager_whisper_class():
    """WhisperSTTService that decodes off the event loop.

    Pipecat 1.12's `run_stt` calls faster-whisper's `transcribe()` in a thread, but
    `transcribe()` returns a lazy generator and the decoding happens in the `for segment
    in segments` loop on the event loop. Each transcription froze the pipeline for ~2 s:
    VAD went deaf, so a caller who resumed after a short pause looked silent and the turn
    ended mid-question (L3, fast / noisy / accented personas). Wrapping the model's
    `transcribe` to return a list moves the decoding into the worker thread.
    """
    from pipecat.services.whisper.stt import WhisperSTTService

    class EagerWhisperSTTService(WhisperSTTService):
        async def run_stt(self, audio: bytes):
            model = getattr(self, "_model", None)
            if model is not None and not getattr(model, "_lab_eager", False):
                lazy = model.transcribe

                def eager(*args: Any, **kwargs: Any):
                    segments, info = lazy(*args, **kwargs)
                    return list(segments), info

                model.transcribe = eager
                model._lab_eager = True
            async for frame in super().run_stt(audio):
                yield frame

    return EagerWhisperSTTService


def build_services(cfg: LabConfig) -> tuple[Any, Any, Any]:
    """VAD, STT and TTS from config `models.stt` / `models.tts`."""
    from pipecat.audio.vad.silero import SileroVADAnalyzer
    from pipecat.processors.audio.vad_processor import VADProcessor

    stt_ref, tts_ref = cfg.models["stt"], cfg.models["tts"]
    stt_extra, tts_extra = stt_ref.model_extra or {}, tts_ref.model_extra or {}
    if stt_ref.provider != "faster-whisper":
        raise ValueError(f"unsupported STT provider: {stt_ref.provider!r}")
    from pipecat.services.whisper.stt import WhisperSTTService

    stt = _eager_whisper_class()(
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

    # The turn processor sits after STT: its end-of-turn strategy waits for a transcript,
    # and frames only flow downstream. Placed before STT it never saw one, and every turn
    # ended on its 5 s give-up timer instead (the first full L3 run's STT p50: 5.0 s).
    return Pipeline([transport.input(), vad, stt, turns, agent_proc, tts,
                     transport.output()])


def task_params(cfg: LabConfig) -> Any:
    from pipecat.pipeline.task import PipelineParams

    return PipelineParams(audio_in_sample_rate=cfg.voice.sample_rate_in,
                          audio_out_sample_rate=cfg.voice.sample_rate_out)
