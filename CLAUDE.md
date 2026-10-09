# CLAUDE.md — efis-voice-lab

The plan is [docs/PLAN.md](docs/PLAN.md). It is a working draft, not a contract.

## Working rules

- Work **one milestone at a time**. A milestone is done when its acceptance criteria pass. Stop and summarize before starting the next one.
- Ragas, Pipecat, FastMCP, and LiveKit change their APIs often. **Check each library's API against the installed version** before writing code that uses it. Don't rely on remembered signatures.
- Keep every model swappable through config: embedding model, agent LLM, judge LLM, STT, TTS. Nothing hard-coded.
- When this plan lists options at a **Decision point**, run the small spike it describes, then ask the maintainer to choose before building further.
- The default target is local and free. Paid or hosted services are optional and controlled by config.
- Don't index `CLAUDE.md` or `STRUCTURAL_REVIEW.md` from the pyEfis repo (see Corpus).

- Commits: focused, signed off (`git commit -s`), no emojis.
