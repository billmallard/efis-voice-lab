"""Split documents into retrievable sections.

- Markdown / reStructuredText: one chunk per heading section; `heading_path` is
  the chain of headings above it.
- YAML: one chunk per top-level key, with the comment block directly above the
  key kept in the chunk (the comments are most of the documentation).

Sections longer than `max_chars` are split on paragraph boundaries (lines for
YAML); sections shorter than `min_chars` -- bare headings, mostly -- are dropped.

Bump CHUNKER_VERSION whenever output changes, so re-ingest replaces old chunks.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field

CHUNKER_VERSION = "1"

_MD_HEADING = re.compile(r"^(#{1,6})\s+(.+?)\s*#*\s*$")
_MD_FENCE = re.compile(r"^\s*(```|~~~)")
_RST_ADORNMENT = re.compile(r"^([=\-`:'\"~^_*+#<>.])\1{2,}\s*$")
_YAML_TOP_KEY = re.compile(r"^(?:\"[^\"]+\"|'[^']+'|[^\s#\-][^:]*?)\s*:(?:\s|$)")


@dataclass
class Section:
    heading_path: list[str]
    lines: list[str] = field(default_factory=list)

    @property
    def text(self) -> str:
        return "\n".join(self.lines).strip()


@dataclass(frozen=True)
class Chunk:
    path: str
    doc_type: str  # markdown | rst | yaml
    heading_path: tuple[str, ...]
    text: str
    part: int = 0  # index within an oversized section that was split


# -- Markdown ----------------------------------------------------------------

def _markdown_sections(text: str) -> list[Section]:
    sections = [Section([])]
    stack: list[tuple[int, str]] = []
    in_fence = False
    for line in text.splitlines():
        if _MD_FENCE.match(line):
            in_fence = not in_fence
        m = None if in_fence else _MD_HEADING.match(line)
        if m:
            level, title = len(m.group(1)), m.group(2).strip()
            while stack and stack[-1][0] >= level:
                stack.pop()
            stack.append((level, title))
            sections.append(Section([t for _, t in stack]))
        else:
            sections[-1].lines.append(line)
    return sections


# -- reStructuredText ---------------------------------------------------------

def _rst_sections(text: str) -> list[Section]:
    """Titles are a non-indented line underlined (optionally overlined) by a run of
    one punctuation character at least as long as the title. Levels follow the
    order in which adornment styles first appear, as in docutils."""
    lines = text.splitlines()
    styles: list[tuple[str, bool]] = []
    sections = [Section([])]
    stack: list[tuple[int, str]] = []
    i = 0
    while i < len(lines):
        line = lines[i]
        over = _RST_ADORNMENT.match(line)
        # Overlined title: adornment / title / same adornment
        if (over and i + 2 < len(lines) and lines[i + 1].strip()
                and lines[i + 2].strip() == line.strip()
                and len(line.strip()) >= len(lines[i + 1].strip())):
            title, style, consumed = lines[i + 1].strip(), (line.strip()[0], True), 3
        elif (line.strip() and not line[0].isspace() and i + 1 < len(lines)
              and (under := _RST_ADORNMENT.match(lines[i + 1]))
              and len(lines[i + 1].rstrip()) >= len(line.rstrip())
              and not _RST_ADORNMENT.match(line)):
            title, style, consumed = line.strip(), (under.group(1), False), 2
        else:
            sections[-1].lines.append(line)
            i += 1
            continue
        if style not in styles:
            styles.append(style)
        level = styles.index(style)
        while stack and stack[-1][0] >= level:
            stack.pop()
        stack.append((level, title))
        sections.append(Section([t for _, t in stack]))
        i += consumed
    return sections


# -- YAML ---------------------------------------------------------------------

def _yaml_sections(text: str) -> list[Section]:
    lines = text.splitlines()
    sections: list[Section] = [Section(["(file header)"])]
    pending_comments: list[str] = []
    for line in lines:
        if line.startswith("#"):
            pending_comments.append(line)
            continue
        m = _YAML_TOP_KEY.match(line)
        if m and line != "---":
            key = line.split(":", 1)[0].strip().strip("'\"")
            sections.append(Section([key], pending_comments + [line]))
            pending_comments = []
            continue
        # Anything else (a blank line included) ends the comment block: only a block
        # contiguous with a key documents that key.
        sections[-1].lines.extend(pending_comments)
        pending_comments = []
        sections[-1].lines.append(line)
    sections[-1].lines.extend(pending_comments)
    return sections


# -- Splitting ----------------------------------------------------------------

def _split(text: str, max_chars: int, by_lines: bool) -> list[str]:
    """Pack paragraphs (or lines) into pieces of at most max_chars; hard-split any
    single unit that is longer on its own."""
    if len(text) <= max_chars:
        return [text]
    sep = "\n" if by_lines else "\n\n"
    units = text.split("\n") if by_lines else re.split(r"\n\s*\n", text)
    pieces: list[str] = []
    current = ""
    for unit in units:
        while len(unit) > max_chars:
            if current:
                pieces.append(current)
                current = ""
            pieces.append(unit[:max_chars])
            unit = unit[max_chars:]
        candidate = f"{current}{sep}{unit}" if current else unit
        if len(candidate) > max_chars and current:
            pieces.append(current)
            current = unit
        else:
            current = candidate
    if current.strip():
        pieces.append(current)
    return [p.strip() for p in pieces if p.strip()]


_PARSERS = {
    ".md": ("markdown", _markdown_sections),
    ".rst": ("rst", _rst_sections),
    ".yaml": ("yaml", _yaml_sections),
    ".yml": ("yaml", _yaml_sections),
}


def chunk_file(path: str, text: str, max_chars: int = 1500, min_chars: int = 40) -> list[Chunk]:
    suffix = "." + path.rsplit(".", 1)[-1].lower()
    doc_type, parse = _PARSERS[suffix]
    chunks: list[Chunk] = []
    for section in parse(text):
        body = section.text
        if len(body) < min_chars:
            continue
        heading_path = tuple(section.heading_path) or ("(top of file)",)
        for part, piece in enumerate(_split(body, max_chars, by_lines=doc_type == "yaml")):
            if len(piece) >= min_chars:
                chunks.append(Chunk(path, doc_type, heading_path, piece, part))
    return chunks
