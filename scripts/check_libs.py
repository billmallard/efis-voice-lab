"""M0 spike: confirm the fast-moving libraries install, import, and expose the APIs we plan on.

Prints installed versions so API checks in later milestones target the right release.
"""

from __future__ import annotations

import importlib
import sys
from importlib.metadata import version

# distribution -> {module: [attributes we will build on]}
CHECKS: dict[str, dict[str, list[str]]] = {
    "ragas": {
        "ragas.metrics.collections": [
            "ContextPrecision", "ContextRecall", "Faithfulness", "AnswerRelevancy",
        ],
    },
    "fastmcp": {"fastmcp": ["FastMCP", "Client"]},
    "pipecat-ai": {
        "pipecat.pipeline.pipeline": ["Pipeline"],
        "pipecat.pipeline.task": ["PipelineTask"],
    },
    "qdrant-client": {"qdrant_client": ["QdrantClient"]},
}


def main() -> int:
    failed = 0
    for dist, modules in CHECKS.items():
        try:
            for mod_name, attrs in modules.items():
                mod = importlib.import_module(mod_name)
                missing = [a for a in attrs if not hasattr(mod, a)]
                if missing:
                    raise AttributeError(f"{mod_name} lacks {missing}")
            print(f"ok    {dist}=={version(dist)}")
        except Exception as e:  # noqa: BLE001 -- report every failure, not just the first
            failed += 1
            print(f"FAIL  {dist}: {type(e).__name__}: {e}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
