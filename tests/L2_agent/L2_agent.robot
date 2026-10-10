*** Settings ***
Documentation       L2 -- the text agent on the golden set (text in, text out).
...
...                 One test per golden case is generated from golden/questions.yaml by
...                 VoiceLabLibrary. Each case asks the text agent the canonical question
...                 (same prompt, LLM and MCP tools as the voice agent, no audio), then
...                 judges the answer: answer / decline / clarify behavior, correctness
...                 against the reference answer, Ragas faithfulness and answer relevancy,
...                 and speakability (judge LLM from config/lab.yaml). Deterministic
...                 checks cover tool use, the agent's search queries, and required or
...                 forbidden terms.
...
...                 Scores land in results/L2/latest.json. Thresholds live in
...                 config/lab.yaml.

Library             lab.harness.VoiceLabLibrary    layer=L2    judge=${JUDGE}
...                     ids=${L2_IDS}    agent_model=${AGENT_MODEL}

Suite Setup         Start Agent
Suite Teardown      Finish L2 Run


*** Variables ***
# Narrow the generated cases: robot -v L2_IDS:HW-003,OOS-001 tests/L2_agent
${L2_IDS}           ${EMPTY}
# auto = judge on when ANTHROPIC_API_KEY is set; on | off to force
${JUDGE}            auto
# Override models.agent_llm.name, e.g. -v AGENT_MODEL:qwen3:8b
${AGENT_MODEL}      ${EMPTY}


*** Test Cases ***
Aggregate Scores Meet Thresholds
    [Documentation]    Behavior accuracy, correctness pass rate, faithfulness, answer
    ...    relevancy and speakability against config thresholds (judge on only).
    [Tags]    aggregate    run-level
    L2 Aggregates Should Meet Thresholds
