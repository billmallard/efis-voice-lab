# Handoff: state of the lab (2026-10-11)

Read this, then [CLAUDE.md](../CLAUDE.md) and [PLAN.md](PLAN.md). The milestone notes in
[notes/](notes/) are the detailed record; this page says where things stand and what is
not written down anywhere else.

## Where it stands

M0 to M5 and D1 are done and merged. The published report is live (D1, #25).

| Layer | What runs it | Reference run |
|---|---|---|
| L1 retrieval | `robot tests/L1_retrieval` | `baselines/L1/2026-10-09.json`: hit rate 0.87, MRR 0.75 |
| L2 text agent | `robot tests/L2_agent` | `baselines/L2/2026-10-10-calibrated.json`: behavior 0.94, correct 0.55, faithful 0.92, speakable 0.66 |
| L3 voice | `robot tests/L3_voice` | `baselines/L3/2026-10-10.json`: STT 94%, 0 split turns, barge-in 376 ms, content pass 16%, 15 s to first audio |

The browser demo is `uv run lab serve-voice`, served at http://localhost:7860 (choose
SmallWebRTC). The maintainer talked to it on 2026-10-11, which was M5's acceptance test.
`uv run lab report` writes the all-layers HTML report to `results/report/index.html`.

**The published report** (D1) is https://efis-voice-lab.pages.dev, built by CI from
`runs/`. After a full run worth showing, publish it from this machine:
`uv run lab publish L1 L2 L3` (L3 audio goes to a `audio-L3-<stamp>` release), then commit
`runs/` and push. Branches deploy to `<branch>.efis-voice-lab.pages.dev`. Publish full runs
only: a slice skews the trend lines. Details in [notes/D1.md](notes/D1.md).

**Next:** the maintainer hasn't chosen. The options:
- **M6:** telephony through Asterisk. It opens with a decision point: how SIP reaches the
  agent. Spike it, then ask.
- **Tuning.** The maintainer said to tune after the milestones are done, not during them.
  Record findings and don't chase them. The report site now shows each tuning run's
  effect against the last, so publish every full run.
- **D2:** the hosted live demo; it shares the Cloudflare Tunnel with M8.

Open issues: #7 M6, #8 M7, #9 M8, #10 M9, #14 D2.

## Working with the maintainer

- One milestone at a time, with a PR per milestone. Merge when CI is green, then stop and
  summarize. Commits are signed off (`-s`), with no emojis.
- He answers decisions in prose. Lay out the options with a recommendation; the
  AskUserQuestion modal is only for genuinely open, side-by-side choices.
- Settled choices; don't re-open them:
  - The judge is Claude Haiku 4.5.
  - The golden set records misleading source docs, and the expected answer follows the
    evidence.
  - Transports: websocket for L3 and M6, SmallWebRTC for the browser and D2.
  - Speakability: markup is the normalizer's job; spoken commands, paths and density
    still fail.
  - The agent model is llama3.1:8b.
- This is a public portfolio repo. Keep anything about his other projects, his employer,
  his network or his home lab out of it.
- Calibration: rubrics were tuned on the same 20 graded cases. A held-out blind sheet
  (about 10 ungraded answer cases) is the honest next check. It's optional, and was
  offered but not yet requested.

## The machine (Windows 10, GTX 1080 Ti)

- Docker (Rancher Desktop, CPU only) runs Qdrant (:6333) and Ollama (:11434), which
  handles embeddings: `docker compose up -d --wait`.
- **The agent LLM runs on a separate, native Ollama on :11435, using Vulkan.** CUDA fails
  because the driver (560.94) is too old for Ollama 0.40's Pascal build. It isn't a
  service, so start it after every reboot:

  ```powershell
  $env:OLLAMA_HOST="127.0.0.1:11435"; $env:CUDA_VISIBLE_DEVICES="-1"; $env:OLLAMA_VULKAN="1"
  & "$env:LOCALAPPDATA\Programs\Ollama\ollama.exe" serve
  ```

  `.env` (gitignored) holds `ANTHROPIC_API_KEY` and `LAB_AGENT_LLM_URL=http://localhost:11435`.
  Check: `curl localhost:11435/api/version`.
- Whisper and Kokoro run on the CPU. Model files cache under `~/.cache/pipecat`; the
  first run downloads them.
- **Stop processes only by exact PID** (for example, the PID owning a port, from
  `Get-NetTCPConnection`). A broad `taskkill` filter can take down the maintainer's
  editor and browser.

## Cost and time of a run

| Run | Time | Judge cost |
|---|---|---|
| L1 full | ~5 min | ~$0.58 |
| L2 full | ~7 min | ~$0.39 |
| L3 full (89 tests) | ~70 min | ~$1.05 |
| `lab calibrate rejudge` (judges only) | ~1 min | ~$0.10 |

Long runs go in the background. Robot buffers its console output, so read progress from
`results/L3/audio/*-reply.wav` timestamps instead. Robot also swallows Pipecat's logs
after the warm-up turn: to debug the voice path, reproduce the problem in a short script
that sends loguru to a file.

## Gotchas not obvious from the code

- **Pipecat 1.12 bugs, worked around locally and not yet reported upstream.** Offer to
  report them; don't file without asking.
  1. `WhisperSTTService` decodes faster-whisper's lazy generator on the event loop, which
     freezes VAD for ~2 s per transcription. Workaround: `EagerWhisperSTTService` in
     `lab/voice/pipeline.py`.
  2. `SmallWebRTCConnection.is_connected()` reports False once the data channel has been
     quiet for 3 s, so a slow-starting bot never starts listening. Workaround:
     `trust_peer_connection` in `lab/voice/serve.py`.
- **Voice pipeline order matters:** VAD, then STT, then `UserTurnProcessor`, then the
  agent. The turn processor's stop strategy waits for a transcript, so it must sit after
  STT. It announces the end of a turn just before forwarding the deciding transcript, so
  `AgentProcessor` answers from a task: after a 150 ms grace period, once every VAD segment
  is transcribed and the caller is not speaking. Late transcripts are dropped.
- **Known L3 edge:** the first case after the warm-up turn can wait about 13 s and lose
  part of a question. Recorded, not fixed.
- **Library APIs move fast; check the installed source.** The ones that bit:
  - `messages.parse` rather than `output_format` on `create`;
  - the Anthropic SDK 1.x dropped `temperature` (pass it in `extra_body`);
  - Ragas 0.4.3 needs `langchain-community<0.4`;
  - in Pipecat 1.12, VAD is a `VADProcessor` stage, not a transport parameter.
- **Haiku ignores rewording of a prose rubric.** Make the judge report findings and derive
  the verdict in code; that's how the correctness judge works now.
- **Attribution is recomputed by `lab report`,** so rule changes need no re-run. The rule
  order, and why it differs from the plan, is in `lab/harness/attribution.py`.
- `lab serve-voice` runs `serve.py` with `runpy` (`alter_sys=True`), because Pipecat's
  runner finds `bot()` on `__main__`.
- Windows `time.monotonic()` ticks in 15.6 ms steps; use `perf_counter` for timing.

## Map

| Path | What |
|---|---|
| `lab/agent/` | Text agent: prompt, LLM providers, MCP tool loop, speech normalizer |
| `lab/voice/` | Pipeline, websocket session and test caller, persona audio, browser server |
| `lab/harness/` | Robot keyword library (L1, L2, L3), judges and rubrics, attribution, calibration, report |
| `golden/` | Golden questions, personas, calibration worksheet and grades |
| `tests/L*_*/` | One Robot suite per layer; tests are generated from the golden set |
| `scripts/` | Spikes and smoke checks (`check_serve_voice.py`, `spike_transport.py`, ...) |
| `docs/notes/M*.md` | Per-milestone record: decisions, findings, baselines |
