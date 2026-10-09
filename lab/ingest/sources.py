"""config/sources.yaml: which repos to index and which files inside them."""

from __future__ import annotations

import hashlib
import json
import re
from functools import cache
from pathlib import Path

import yaml
from pydantic import BaseModel

from lab.config import ROOT

DEFAULT_PATH = ROOT / "config" / "sources.yaml"
PARSED_SUFFIXES = {".md", ".rst", ".yaml", ".yml"}


class Source(BaseModel):
    repo: str  # owner/name on GitHub
    branch: str
    origin: str  # fork | upstream
    paths: list[str]

    @property
    def id(self) -> str:
        return f"{self.repo}@{self.branch}"

    @property
    def clone_url(self) -> str:
        return f"https://github.com/{self.repo}.git"


class SourcesConfig(BaseModel):
    exclude: list[str] = []
    sources: list[Source]


def load(path: str | Path | None = None) -> SourcesConfig:
    raw = yaml.safe_load(Path(path or DEFAULT_PATH).read_text(encoding="utf-8"))
    return SourcesConfig.model_validate(raw)


@cache
def _glob_re(pattern: str) -> re.Pattern[str]:
    """Translate a repo-relative glob: `**` crosses directories, `*` and `?` do not."""
    out, i = [], 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            out.append("(?:.*/)?")
            i += 3
        elif pattern.startswith("**", i):
            out.append(".*")
            i += 2
        elif pattern[i] == "*":
            out.append("[^/]*")
            i += 1
        elif pattern[i] == "?":
            out.append("[^/]")
            i += 1
        else:
            out.append(re.escape(pattern[i]))
            i += 1
    return re.compile("".join(out) + r"\Z")


def matches(rel_path: str, pattern: str) -> bool:
    if "/" not in pattern and not any(c in pattern for c in "*?"):
        return rel_path.rsplit("/", 1)[-1] == pattern  # bare name: basename anywhere
    return bool(_glob_re(pattern).match(rel_path))


def is_excluded(rel_path: str, exclude: list[str]) -> bool:
    return any(matches(rel_path, p) for p in exclude)


def select_files(root: Path, source: Source, exclude: list[str]) -> list[str]:
    """Repo-relative POSIX paths of the files to index, sorted."""
    selected = []
    for f in root.rglob("*"):
        if not f.is_file() or f.suffix.lower() not in PARSED_SUFFIXES:
            continue
        rel = f.relative_to(root).as_posix()
        if rel.startswith(".git/") or is_excluded(rel, exclude):
            continue
        # Top-level paths without a glob are exact file paths, not basenames.
        if any(rel == p or (any(c in p for c in "*?") and matches(rel, p)) for p in source.paths):
            selected.append(rel)
    return sorted(selected)


def fingerprint(source: Source, exclude: list[str], chunker_version: str) -> str:
    """Changes whenever what would be indexed for a commit could change."""
    blob = json.dumps(
        {"paths": source.paths, "exclude": exclude, "origin": source.origin,
         "chunker": chunker_version},
        sort_keys=True,
    )
    return hashlib.sha256(blob.encode()).hexdigest()[:12]
