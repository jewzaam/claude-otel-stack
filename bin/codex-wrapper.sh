#!/usr/bin/env bash
# Launch Codex with a project identity that remains stable across /cd.
#
# The hook payload's cwd follows the current Codex directory. Capture the
# launch directory separately so hooks have a project identity that survives
# /cd.

set -euo pipefail

export CODEX_PROJECT="${CODEX_PROJECT:-$PWD}"

# Keep this available if Codex's native exporter honors standard OTEL resource
# attributes. Hook-based exporters can use CODEX_PROJECT directly either way.
export OTEL_RESOURCE_ATTRIBUTES="${OTEL_RESOURCE_ATTRIBUTES:+${OTEL_RESOURCE_ATTRIBUTES},}project=${CODEX_PROJECT}"

exec codex "$@"
