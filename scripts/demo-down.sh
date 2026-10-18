#!/usr/bin/env bash
# Take the demo down (docs/demo.md): stops what `demo up` and `demo run` started and shelves the
# demo sandboxes after asking (--yes: without asking).
set -euo pipefail
cd "$(dirname "$0")/.."
exec uv run --extra agent --extra hub python -m mechatronikers.demo down "$@"
