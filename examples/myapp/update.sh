#!/usr/bin/env bash
set -euo pipefail
exec /usr/local/bin/rpm-repo-sync --repo myapp --keep 3 "$@"
