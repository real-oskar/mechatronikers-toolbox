#!/usr/bin/env bash
# Create or update the repository's label set (idempotent). Source of truth for labels;
# documented in docs/workflow.md. Usage: scripts/sync-labels.sh [owner/repo]
set -euo pipefail

repo="${1:-$(gh repo view --json nameWithOwner -q .nameWithOwner)}"

# name|color|description
labels=(
  "type:feature|1d76db|New functionality"
  "type:bug|d73a4a|Something is broken"
  "type:docs|0075ca|Documentation only"
  "type:chore|c5def5|Maintenance, tooling, dependencies, CI"
  "type:test|bfd4f2|Tests or evaluation only"
  "type:research|d4c5f9|Spike or investigation; result is a written finding"

  "area:spec|fbca04|Shared Pydantic contract (spec, telemetry schema)"
  "area:drawing-mcp|f9d0c4|Drawing analysis MCP server"
  "area:cad-mcp|f9d0c4|CAD MCP server, build runner, validation, export"
  "area:agent|f9d0c4|LangGraph agent, prompts, model config"
  "area:review-app|f9d0c4|Review web app"
  "area:infra|f9d0c4|Sandbox, session manager, collector, storage"
  "area:eval|f9d0c4|Evaluation runner, metrics, test data"
  "area:data|f9d0c4|Sample and test data"

  "phase:1|0e8a16|Phase 1 - MVP"
  "phase:2|5ebd3e|Phase 2 - robustness and lifecycle"
  "phase:3|a2d9a0|Phase 3 - stretch"

  "priority:high|b60205|Blocks others or the phase goal"
  "priority:medium|e99695|Normal priority"
  "priority:low|f4cccc|Nice to have"

  "status:ready|ededed|Specified and ready to start"
  "status:in-progress|fef2c0|Someone is working on it"
  "status:in-review|c2e0c6|PR open, waiting for review"
  "status:blocked|000000|Waiting on something else; say what in a comment"

  "v2|0052cc|V2 - local models and fine-tuning (separate stage after V1)"

  "needs-decision|5319e7|Touches an open point; a human must decide first"
)

for entry in "${labels[@]}"; do
  IFS='|' read -r name color description <<<"$entry"
  gh label create "$name" --repo "$repo" --color "$color" --description "$description" --force
done

# Default GitHub labels replaced by type:* labels. Deleting a label removes it from issues.
for name in bug documentation enhancement; do
  gh label delete "$name" --repo "$repo" --yes 2>/dev/null || true
done
