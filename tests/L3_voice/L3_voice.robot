*** Settings ***
Documentation       L3 -- the voice agent on the golden set (audio in, audio out).
...
...                 Golden questions are synthesized as caller audio (golden/personas.yaml)
...                 and spoken in real time to the voice pipeline over its websocket
...                 transport: Silero VAD, faster-whisper, the same text agent as L2, and
...                 Kokoro. Each case checks that STT heard the question, that one question
...                 stayed one turn, and the L2 behavior, correctness, faithfulness and
...                 speakability judgments on the answer, then records a failure
...                 attribution tag. Behavior tests script barge-in and caller silence.
...
...                 Scores and attribution land in results/L3/latest.json; both sides'
...                 audio in results/L3/audio/.

Library             lab.harness.VoiceLabLibrary    layer=L3    judge=${JUDGE}
...                     ids=${L3_IDS}    groups=${L3_GROUPS}
...                     persona_cases=${PERSONA_CASES}    personas=${PERSONAS}

Suite Setup         Start Voice Session
Suite Teardown      Finish L3 Run


*** Variables ***
# Narrow the run: -v L3_IDS:HW-003,OOS-001   -v L3_GROUPS:variants
${L3_IDS}           ${EMPTY}
${L3_GROUPS}        variants,personas,behavior
${PERSONA_CASES}    HW-003,CTRL-004,SVS-001,OOS-001
${PERSONAS}         fast,accent_gb,accent_es,cockpit_noise,hesitant
# auto = judge on when ANTHROPIC_API_KEY is set; on | off to force
${JUDGE}            auto


*** Test Cases ***
Time To First Audio Is Within Budget
    [Documentation]    Median time from the caller going quiet to the bot's first audio,
    ...    against thresholds.time_to_first_audio_ms.
    [Tags]    latency    run-level
    Time To First Audio Should Be Within Budget
