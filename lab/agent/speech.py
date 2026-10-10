"""Speech normalization: the deterministic cleanup between the LLM's text and TTS.

It removes what a speech synthesizer would otherwise read as noise (markdown, backticks,
list markers, link syntax) and says common units and symbols as words. It doesn't
rewrite content: a shell command or a file path survives, because describing it
instead is a judgment call for the prompt or a rewrite pass, and the speakability
judge still fails it. L2 judges speakability on this output, and the M5 voice agent
sends the same output to TTS.
"""

from __future__ import annotations

import re

_UNITS = [
    (r"(\d)\s?TB\b", r"\1 terabytes"),
    (r"(\d)\s?GB\b", r"\1 gigabytes"),
    (r"(\d)\s?MB\b", r"\1 megabytes"),
    (r"(\d)\s?KB\b", r"\1 kilobytes"),
    (r"(\d)\s?ms\b", r"\1 milliseconds"),
    (r"\s?°\s?C\b", " degrees Celsius"),
    (r"\s?°\s?F\b", " degrees Fahrenheit"),
    (r"\s?°", " degrees"),
    (r"\binHg\b", "inches of mercury"),
    (r"\bhPa\b", "hectopascals"),
    (r"±\s?", "plus or minus "),
    (r"~\s?(?=\d)", "about "),
    (r"\s?⇄\s?|\s?↔\s?", " and "),
    (r"\s&\s", " and "),
    (r"\s?->\s?|\s?→\s?", " to "),
]


def normalize(text: str) -> str:
    if not text:
        return text
    t = re.sub(r"```[a-zA-Z0-9_-]*\n?", "", text)                 # code fences
    t = re.sub(r"`([^`]*)`", r"\1", t)                              # inline code
    t = re.sub(r"\[([^\]]+)\]\((?:https?://)?[^)]+\)", r"\1", t)    # [text](url)
    t = re.sub(r"<?https?://[^\s>]*[^\s>.,;:)]>?", "a link", t)      # bare URLs
    t = re.sub(r"(\*\*|__)(.+?)\1", r"\2", t)                      # bold
    t = re.sub(r"(?<![\w*])\*(?!\s)([^*\n]+?)(?<!\s)\*(?![\w*])", r"\1", t)  # italic
    t = re.sub(r"(?m)^\s{0,3}#{1,6}\s*", "", t)                     # headings
    lines = []
    for line in t.splitlines():
        line = re.sub(r"^\s*(?:[-*+•]|\d+[.)])\s+", "", line).strip()  # list markers
        if not line:
            continue
        # A list item or heading becomes its own sentence.
        if lines and not re.search(r"[.!?:;,]$", lines[-1]):
            lines[-1] += "."
        lines.append(line)
    t = " ".join(lines)
    for pattern, repl in _UNITS:
        t = re.sub(pattern, repl, t)
    return re.sub(r"\s{2,}", " ", t).strip()
