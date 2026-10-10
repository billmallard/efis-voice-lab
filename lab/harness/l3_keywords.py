"""L3 keywords for VoiceLabLibrary: call the voice agent with synthesized caller audio.

One voice session (websocket transport, lab/voice/caller.py) and one open caller line
serve the whole suite, like a long phone call. Each test speaks one golden question as a
persona, waits for the spoken reply, then scores it with the L2 judges plus voice checks:
did STT hear the question, did one utterance stay one turn, and how long did the caller
wait for audio. Behavior scenarios script barge-in and silence.

Result keys are "CASE|variant|persona" so the L2 keywords (`Judge Answer`, `Agent Should
...`) run unchanged on L3 records.
"""

from __future__ import annotations

import asyncio
import time
from typing import Any

import numpy as np
import soundfile as sf
from robot.api import logger
from robot.api.deco import keyword

from lab.harness import attribution, l2, l3


class L3Keywords:
    # Provided by VoiceLabLibrary: cfg, cases, judge_enabled, _run, RESULTS_DIR, l2_results,
    # _turns, _judges, _case

    def _l3_init(self) -> None:
        self._voice_cm = None
        self._caller = None
        self._log = None
        self._synth = None
        self._personas = None
        self._said: dict[str, str] = {}

    def _l3_key(self, case_id: str, variant: int, persona: str) -> str:
        return f"{case_id}|v{variant}|{persona}"

    def line_for(self, case_id: str, variant: int) -> str:
        """Variant 0 is the canonical question; 1.. are the golden spoken_variants."""
        case = self.cases[case_id]
        return case.question if variant == 0 else case.spoken_variants[variant - 1]

    # ------------------------------------------------------------ lifecycle

    @keyword
    def start_voice_session(self, port: int = 8791) -> None:
        """Start the voice pipeline (websocket), open one caller line, warm everything up."""
        from lab.voice.audio_synth import Synth, load_personas
        from lab.voice.caller import WsCaller, voice_session

        self.start_agent()                       # the L2 agent and judges, shared
        self._synth, self._personas = Synth(self.cfg.voice.sample_rate_in), load_personas()
        self._voice_cm = voice_session(self.cfg, int(port), agent=self._agent)
        url, self._log = self._run(self._voice_cm.__aenter__(), timeout=120)
        self._caller = WsCaller(url, self.cfg.voice.sample_rate_in,
                                self.cfg.voice.sample_rate_out)
        self._run(self._caller.__aenter__())
        # Warm-up turn: model loads (Whisper, Kokoro) aren't charged to a golden case.
        pcm = self._synth.render("Hello, can you hear me?", self._personas["clean_m"])
        _, end = self._run(self._caller.say(pcm))
        self._run(self._caller.wait_for_reply(end, timeout=120), timeout=180)
        logger.info(f"voice session on {url}; warm-up turn: "
                    f"{self._log.turns[-1].timing_ms() if self._log.turns else None}",
                    also_console=True)

    @keyword
    def finish_l3_run(self) -> str:
        try:
            budget = self.cfg.thresholds.get("time_to_first_audio_ms")
            recs = list(self.l2_results.values())
            for r in recs:
                if r.get("kind", "turn") == "turn":
                    r["attribution"] = attribution.attribute(
                        self._case(r["id"]), r, self.cfg.thresholds, budget)
            ref = self.cfg.models
            extra = {
                "voice": {"stt": ref["stt"].model_dump(), "tts": ref["tts"].model_dump(),
                          **self.cfg.voice.model_dump(), "transport": "websocket"},
                "agent": {"provider": ref["agent_llm"].provider,
                          "model": ref["agent_llm"].name, "prompt_sha": self._prompt_sha()},
                "aggregate": l3.aggregate(recs, budget),
                "cases": recs,
            }
            path = self._write_report("L3", extra)
            agg = extra["aggregate"]
            logger.info(f"turns {agg['turns']}, WER {agg['stt_wer_mean']}, "
                        f"attribution {agg['attribution']}, latency {agg['latency_ms']}",
                        also_console=True)
            return path
        finally:
            if self._caller is not None:
                self._run(self._caller.__aexit__(None, None, None))
            if self._voice_cm is not None:
                self._run(self._voice_cm.__aexit__(None, None, None), timeout=60)
            if self._agent_cm is not None:
                self._run(self._agent_cm.__aexit__(None, None, None))
                self._agent_cm = None

    # ---------------------------------------------------------------- turns

    def _save(self, name: str, pcm: np.ndarray, rate: int) -> str:
        path = self.RESULTS_DIR / "L3" / "audio" / f"{name.replace('|', '_')}.wav"
        path.parent.mkdir(parents=True, exist_ok=True)
        sf.write(path, pcm, rate)
        return str(path.relative_to(self.RESULTS_DIR.parent).as_posix())

    @keyword
    def call_agent(self, case_id: str, variant: int, persona: str) -> str:
        """Speak one golden line as a persona and wait for the spoken reply. Records the
        transcript, the agent turn, stage timings and both sides' audio."""
        from lab.agent.text_agent import AgentTurn

        variant = int(variant)
        key = self._l3_key(case_id, variant, persona)
        case, line = self.cases[case_id], self.line_for(case_id, variant)
        pcm = self._synth.render(line, self._personas[persona])
        n_before = len(self._log.turns)
        _, end = self._run(self._caller.say(pcm), timeout=120)
        first, last = self._run(self._caller.wait_for_reply(end, timeout=90, quiet=3.0),
                                timeout=150)
        new = [t for t in self._log.turns[n_before:] if t.kind == "answer"]
        answered = [t for t in new if t.agent is not None]
        main = answered[0] if answered else (new[0] if new else None)
        agent_turn = main.agent if main and main.agent else AgentTurn(
            question="", answer="", model=self._agent.llm.name, stop="no_transcript",
            rounds=0, tool_calls=[], contexts=[], retrieved=[],
            timing={"total_ms": 0.0, "llm_ms": 0.0, "tool_ms": 0.0, "first_llm_ms": 0.0},
            usage={"input_tokens": 0, "output_tokens": 0})
        self._turns[key] = agent_turn
        rec = l2.evaluate(case, agent_turn)
        rec.update(id=key, kind="turn", case_id=case_id, variant=variant, persona=persona,
                   said=line, transcript=" / ".join(t.transcript for t in new),
                   fragments=[f for t in new for f in t.fragments],
                   turn_split=len(new) > 1, turns=len(new))
        rec["turn_taking_failed"] = rec["turn_split"] or not new
        voice = main.timing_ms() if main else {}
        # The caller's experience is when audio arrives; the server stamps "bot started"
        # when its output transport considers audio played, up to ~2 s later over
        # websocket. The server's stage breakdown stays as diagnostics.
        voice["server_time_to_first_audio_ms"] = voice.pop("time_to_first_audio_ms", None)
        rec["timing"] = {**rec["timing"], **voice,
                         "time_to_first_audio_ms":
                             round((first - end) * 1000, 1) if first else None}
        if case.gold_sources:
            res = self._run(self._agent.mcp.call_tool(
                "search_docs", {"query": case.question, "k": self.cfg.agent.search_k}))
            rec["canonical_gold_hit"] = l2.gold_hit(case, res.structured_content["hits"])
        rec["stt"] = {"wer": l3.wer(line, rec["transcript"])}
        rec["audio"] = {"caller": self._save(f"{key}-caller", pcm, self._caller.rate_in),
                        "reply": self._save(f"{key}-reply",
                                            self._caller.received.audio_between(
                                                end, (last or end) + 0.5),
                                            self._caller.rate_out) if first else None}
        self.l2_results[key] = rec
        self._said[key] = line
        logger.info(f"SAID ({persona}): {line}")
        logger.info(f"HEARD: {rec['transcript']}  (WER {rec['stt']['wer']})")
        logger.info(f"ANSWER: {agent_turn.spoken or agent_turn.answer}")
        logger.info(f"timing: {rec['timing']}")
        return rec["transcript"]

    @keyword
    def judge_voice_turn(self, key: str) -> None:
        """The L2 judges on the agent's answer, plus STT equivalence of the transcript to
        what the caller actually said. (Not to the canonical question: spoken variants are
        deliberately different phrasings, so that comparison blamed STT for the golden
        set's own paraphrases.)"""
        self.judge_answer(key)
        if not self.judge_enabled:
            return
        rec = self.l2_results[key]
        try:
            verdict = self._run(self._judges.stt_equivalence(self._said[key],
                                                             rec["transcript"] or "(nothing)"))
            rec["stt"].update(verdict)
        except Exception as e:  # noqa: BLE001 -- a judge failure must not stop the case
            logger.warn(f"{key} stt judge failed: {e!r}")

    @keyword
    def transcript_should_match_question(self, key: str) -> None:
        rec = self.l2_results[key]
        wer_max = self.cfg.thresholds.get("stt_wer_max", 0.25)
        problems = []
        verdict = rec["stt"].get("verdict")
        if verdict == "different":
            problems.append(f"means something else: {rec['stt'].get('reason')}")
        elif verdict is None and rec["stt"]["wer"] > wer_max:     # no judge: WER decides
            problems.append(f"WER {rec['stt']['wer']} > {wer_max}")
        if problems:
            raise AssertionError(f"heard {rec['transcript']!r} for {self._said[key]!r}: "
                                 + "; ".join(problems))

    @keyword
    def utterance_should_be_one_turn(self, key: str) -> None:
        rec = self.l2_results[key]
        if not rec["turns"]:
            raise AssertionError("no turn: nothing was transcribed")
        if rec["turn_split"]:
            raise AssertionError(f"one question became {rec['turns']} turns: "
                                 f"{rec['transcript']!r}")

    @keyword
    def record_attribution(self, key: str) -> None:
        """Logs the failure tag this case would get. Never fails."""
        budget = self.cfg.thresholds.get("time_to_first_audio_ms")
        rec = self.l2_results[key]
        rec["attribution"] = attribution.attribute(self._case(key), rec,
                                                   self.cfg.thresholds, budget)
        logger.info(f"attribution: {rec['attribution']}", also_console=False)

    # ------------------------------------------------------------ behaviors

    @keyword
    def bot_should_stop_when_interrupted(self, case_id: str, after_s: float = 1.5,
                                         max_ms: float = 1500) -> None:
        """Ask a question; once the reply has played for `after_s`, the caller cuts in.
        The bot's audio has to stop within `max_ms` of the caller starting to speak."""
        key = f"{case_id}|barge-in"
        q = self._synth.render(self.cases[case_id].question, self._personas["clean_m"])
        cut = self._synth.render("Wait, hold on.", self._personas["clean_f"])
        _, end = self._run(self._caller.say(q), timeout=120)
        first = self._run(self._caller.wait_for_speech(end, timeout=90), timeout=120)
        rec: dict[str, Any] = {"id": key, "kind": "behavior", "scenario": "barge-in",
                               "case_id": case_id}
        if first is None:
            rec.update(ok=False, detail="the bot never answered the question")
        else:
            self._run(asyncio.sleep(max(0.0, first + float(after_s) - time.perf_counter())))
            t_cut, _ = self._run(self._caller.say(cut), timeout=60)
            # Last bot speech frame belonging to the interrupted answer: speech before
            # a gap of at least 0.6 s after the cut-in.
            self._run(asyncio.sleep(4))
            frames = [t for t, _ in self._caller.received.speech_after(t_cut)]
            stop = t_cut
            for t in frames:
                if t - stop > 0.6:
                    break
                stop = t
            stop_ms = round((stop - t_cut) * 1000)
            rec.update(ok=stop_ms <= float(max_ms), stop_ms=stop_ms,
                       detail=f"bot audio stopped {stop_ms} ms after the caller cut in")
        self._run(self._caller.wait_for_reply(time.perf_counter(), timeout=60), timeout=90)
        self.l2_results[key] = rec
        logger.info(rec["detail"])
        if not rec["ok"]:
            raise AssertionError(rec["detail"])

    @keyword
    def bot_should_reprompt_after_silence(self, slack_s: float = 6.0) -> None:
        """After a finished turn, the caller says nothing. The bot should re-prompt within
        voice.idle_timeout_s (+ `slack_s`)."""
        idle = self.cfg.voice.idle_timeout_s
        t0 = time.perf_counter()
        n_before = len(self._log.turns)
        heard = self._run(self._caller.wait_for_speech(t0, timeout=idle + float(slack_s)),
                          timeout=idle + 60)
        prompts = [t for t in self._log.turns[n_before:] if t.kind == "idle_prompt"]
        rec: dict[str, Any] = {"id": "idle-reprompt", "kind": "behavior",
                               "scenario": "silence", "ok": bool(heard and prompts)}
        rec["detail"] = (f"re-prompted after {heard - t0:.1f} s of silence" if heard
                         else f"no re-prompt within {idle + float(slack_s):.0f} s")
        if heard:
            self._run(self._caller.wait_for_reply(heard - 0.1, timeout=60), timeout=90)
        self.l2_results[rec["id"]] = rec
        logger.info(rec["detail"])
        if not rec["ok"]:
            raise AssertionError(rec["detail"])

    @keyword
    def time_to_first_audio_should_be_within_budget(self) -> None:
        budget = self.cfg.thresholds.get("time_to_first_audio_ms")
        agg = l3.aggregate(list(self.l2_results.values()), budget)
        p50 = agg["latency_ms"]["time_to_first_audio_p50"]
        logger.info(f"latency: {agg['latency_ms']}")
        if p50 is None or (budget and p50 > budget):
            raise AssertionError(f"time to first audio p50 {p50} ms > budget {budget} ms "
                                 f"(stt {agg['latency_ms']['stt_p50']}, agent "
                                 f"{agg['latency_ms']['agent_p50']}, tts "
                                 f"{agg['latency_ms']['tts_first_audio_p50']})")
