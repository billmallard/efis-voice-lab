"""Published runs: the history the report site is built from (D1).

    uv run lab publish L1 L2 L3          # on the lab host, after a run
    uv run lab publish L3 --from results/L3/run-20261010T184544Z.json --no-audio

A run is published by copying its JSON to `runs/<layer>/<stamp>.json`, which is committed.
CI renders the site from that directory, so the site only changes through a commit and
a regression on a branch shows up on that branch's preview. Full runs need the lab host
(the GPU agent, the judge's API key), so CI publishes runs; it doesn't make them.

Audio stays out of git. An L3 run's WAVs (about 90 MB) are encoded to 40 kbps MP3 (about 11 MB) and
attached to a GitHub release tagged `audio-L3-<stamp>`. The site build fetches that release
for the run it drills into. The encoded files are also kept under `results/audio-cache/`,
so a local site build doesn't need to download them.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import zipfile
from datetime import datetime
from pathlib import Path
from typing import Any

from lab.config import ROOT

RUNS = ROOT / "runs"
CACHE = ROOT / "results" / "audio-cache"
LAYERS = ("L1", "L2", "L3")
ASSET = "audio.zip"


def stamp(run: dict[str, Any]) -> str:
    return datetime.fromisoformat(run["started_at"]).strftime("%Y%m%dT%H%M%SZ")


def audio_tag(layer: str, run: dict[str, Any]) -> str:
    return f"audio-{layer}-{stamp(run)}"


def audio_sources(run: dict[str, Any]) -> list[str]:
    """Repo-relative WAV paths the run's records point at, caller and reply."""
    return sorted({src for r in run.get("cases", [])
                   for src in (r.get("audio") or {}).values() if src})


def mp3_name(src: str) -> str:
    return Path(src).with_suffix(".mp3").name


def encode_audio(run: dict[str, Any], dest: Path) -> list[Path]:
    """Encode the run's WAVs to mono MP3 in `dest`. Missing WAVs are skipped."""
    import soundfile as sf

    dest.mkdir(parents=True, exist_ok=True)
    out = []
    for src in audio_sources(run):
        wav = ROOT / src
        if not wav.is_file():
            continue
        data, rate = sf.read(wav, dtype="int16")
        mp3 = dest / mp3_name(src)
        # 40 kbps constant: clear speech, and a whole run stays near 11 MB.
        sf.write(mp3, data, rate, format="MP3", subtype="MPEG_LAYER_III",
                 bitrate_mode="CONSTANT", compression_level=0.8)
        out.append(mp3)
    return out


def _gh(*args: str, check: bool = True) -> subprocess.CompletedProcess:
    return subprocess.run(["gh", *args], cwd=ROOT, capture_output=True, text=True,
                          check=check)


def upload_audio(tag: str, files: list[Path], run: dict[str, Any]) -> None:
    with tempfile.TemporaryDirectory() as tmp:
        bundle = Path(tmp) / ASSET
        with zipfile.ZipFile(bundle, "w", zipfile.ZIP_STORED) as z:
            for f in files:
                z.write(f, f.name)
        if _gh("release", "view", tag, check=False).returncode == 0:
            _gh("release", "upload", tag, str(bundle), "--clobber")
            return
        notes = (f"Caller and agent audio for the L3 run started {run['started_at']} "
                 f"at lab commit {run['lab_commit']}. Used by the published report; "
                 "not a software release.")
        _gh("release", "create", tag, str(bundle), "--title", tag, "--notes", notes,
            "--latest=false")


def fetch_audio(layer: str, run: dict[str, Any], dest: Path) -> int:
    """Put the run's MP3s in `dest`, from the local cache or the GitHub release.

    Returns the number of files placed; 0 when the run has no published audio."""
    tag = audio_tag(layer, run)
    cache = CACHE / tag
    if not any(cache.glob("*.mp3")):
        with tempfile.TemporaryDirectory() as tmp:
            got = _gh("release", "download", tag, "--pattern", ASSET, "--dir", tmp,
                      check=False)
            if got.returncode != 0:
                print(f"site: no audio for {tag}: {got.stderr.strip()}")
                return 0
            cache.mkdir(parents=True, exist_ok=True)
            with zipfile.ZipFile(Path(tmp) / ASSET) as z:
                z.extractall(cache)
    dest.mkdir(parents=True, exist_ok=True)
    n = 0
    for f in cache.glob("*.mp3"):
        shutil.copyfile(f, dest / f.name)
        n += 1
    return n


def publish(layer: str, src: Path | None = None, audio: bool = True) -> Path:
    src = src or ROOT / "results" / layer / "latest.json"
    raw = src.read_bytes()
    run = json.loads(raw)
    if run.get("layer", layer) != layer:
        raise SystemExit(f"publish: {src} is an {run['layer']} run, not {layer}")
    dest = RUNS / layer / f"{stamp(run)}.json"
    dest.parent.mkdir(parents=True, exist_ok=True)
    dest.write_bytes(raw)  # byte for byte: text mode would add CRLF on Windows
    if audio and audio_sources(run):
        tag = audio_tag(layer, run)
        files = encode_audio(run, CACHE / tag)
        if files:
            upload_audio(tag, files, run)
            mb = sum(f.stat().st_size for f in files) / 1e6
            print(f"publish: {len(files)} audio files ({mb:.1f} MB) -> release {tag}")
    return dest


def load_runs(layer: str, root: Path = RUNS) -> list[dict[str, Any]]:
    """Published runs of one layer, oldest first."""
    runs = [json.loads(p.read_text(encoding="utf-8"))
            for p in sorted((root / layer).glob("*.json"))]
    return sorted(runs, key=lambda r: r["started_at"])
