#!/usr/bin/env bash
# Bring the demo up (docs/demo.md). Arguments go to `python -m mechatronikers.demo up`,
# e.g. --rebuild-image, --no-hub, --image localhost/mechatronikers:demo.
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run --extra agent --extra hub python -m mechatronikers.demo up "$@"
