"""The published report site (D1): the report, rendered from every published run.

    uv run lab site                       -> site/index.html (+ audio/)

CI runs this on every push and deploys `site/` to Cloudflare Pages: `main` is the public
site, and any other branch gets a preview. Runs come from `runs/` (see lab.harness.publish);
the newest run of each layer is drilled into, and its audio comes from the run's release.
"""

from __future__ import annotations

import os
import shutil
from pathlib import Path

from lab.config import ROOT, LabConfig
from lab.harness import publish, report

OUT = ROOT / "site"
REPO_URL = "https://github.com/billmallard/efis-voice-lab"


def mp3_resolver(audio_dir: Path) -> report.AudioResolver:
    def resolve(src: str) -> str | None:
        name = publish.mp3_name(src)
        return f"audio/{name}" if (audio_dir / name).is_file() else None
    return resolve


def footer(repo_url: str) -> str:
    sha = os.environ.get("GITHUB_SHA", "")
    branch = os.environ.get("GITHUB_REF_NAME", "")
    built = (f"Built by CI from <a href=\"{repo_url}/commit/{sha}\"><code>{sha[:7]}</code>"
             f"</a> on {report.e(branch)}. ") if sha else "Built locally. "
    return (f"{built}The suites run on the lab host (the agent model runs on a local GPU); "
            f"CI publishes the committed run records and does not re-run them. "
            f"<a href=\"{repo_url}\">Source and method</a>.")


def build(cfg: LabConfig, out: Path = OUT, runs_root: Path = publish.RUNS) -> Path:
    history = {layer: runs for layer in publish.LAYERS
               if (runs := publish.load_runs(layer, runs_root))}
    if not history:
        raise SystemExit(f"site: no published runs under {runs_root}")
    if out.exists():
        shutil.rmtree(out)
    audio_dir = out / "audio"
    if "L3" in history:
        n = publish.fetch_audio("L3", history["L3"][-1], audio_dir)
        print(f"site: {n} audio files for the latest L3 run")
    repo_url = (f"https://github.com/{os.environ['GITHUB_REPOSITORY']}"
                if os.environ.get("GITHUB_REPOSITORY") else REPO_URL)
    return report.build(cfg, out, history=history, audio=mp3_resolver(audio_dir),
                        repo_url=repo_url, footer=footer(repo_url))
