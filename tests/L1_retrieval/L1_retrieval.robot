*** Settings ***
Documentation       L1 -- retrieval quality on the golden set.
...
...                 One test per answerable golden case is generated from
...                 golden/questions.yaml by VoiceLabLibrary. Each case queries the
...                 REST API, checks that the MCP server agrees, scores the retrieved
...                 context with Ragas (judge LLM from config/lab.yaml), and asserts that
...                 a gold source is in the top k, that no stale "trap" document
...                 outranks it, and, for version questions, that the top hit comes from
...                 the right origin (fork or upstream).
...
...                 Scores land in results/L1/latest.json. Thresholds live in
...                 config/lab.yaml.

Library             lab.harness.VoiceLabLibrary    layer=L1    ragas=${RAGAS}
...                     repo=${L1_REPO}    ids=${L1_IDS}

Suite Setup         Start Retrieval Services
Suite Teardown      Finish L1 Run


*** Variables ***
# Narrow the generated cases: robot -v L1_REPO:makerplane/FIX-Gateway  or  -v L1_IDS:HW-003,VER-001
${L1_REPO}          ${EMPTY}
${L1_IDS}           ${EMPTY}
# auto = judge on when ANTHROPIC_API_KEY is set; on | off to force
${RAGAS}            auto


*** Test Cases ***
Retrieval Latency Is Within Budget
    [Documentation]    REST round-trip p95 across every query in this run
    ...    (retrieval.p95_budget_ms).
    [Tags]    latency    run-level
    Retrieval P95 Should Be Within Budget

Aggregate Scores Meet Thresholds
    [Documentation]    Hit rate and MRR, and Ragas context recall when a judge is
    ...    configured, against config thresholds. Ragas context precision is
    ...    reported but not gated (M4 calibration).
    [Tags]    aggregate    run-level
    L1 Aggregates Should Meet Thresholds
