"""Shallow clones of each source, cached under `ingest.cache_dir`."""

from __future__ import annotations

import subprocess
from pathlib import Path

from lab.ingest.sources import Source


def _git(*args: str, cwd: Path | None = None) -> str:
    return subprocess.run(
        ["git", *args], cwd=cwd, check=True, capture_output=True, text=True
    ).stdout.strip()


def checkout(source: Source, cache_dir: Path) -> tuple[Path, str]:
    """Bring the cached clone to the tip of the source branch; return (path, commit sha).

    The cache is ours alone, so it is reset hard to the fetched tip.
    """
    dest = cache_dir / source.id.replace("/", "__")
    if not (dest / ".git").exists():
        dest.parent.mkdir(parents=True, exist_ok=True)
        _git("clone", "--quiet", "--depth", "1", "--single-branch",
             "--branch", source.branch, source.clone_url, str(dest))
    else:
        _git("fetch", "--quiet", "--depth", "1", "origin", source.branch, cwd=dest)
        _git("reset", "--quiet", "--hard", "FETCH_HEAD", cwd=dest)
    return dest, _git("rev-parse", "HEAD", cwd=dest)
