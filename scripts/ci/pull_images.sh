#!/usr/bin/env bash
# Pull compose images one at a time with backoff. Docker Hub auth from CI
# runners times out intermittently, and a parallel `compose pull` aborts every
# image when one fails.
set -euo pipefail
for img in $(docker compose config --images); do
  for attempt in 1 2 3 4 5; do
    docker pull -q "$img" && continue 2
    echo "pull $img failed (attempt $attempt); retrying in $((attempt * 20))s" >&2
    sleep $((attempt * 20))
  done
  echo "giving up on $img" >&2
  exit 1
done
