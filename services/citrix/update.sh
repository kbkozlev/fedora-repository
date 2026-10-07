#!/usr/bin/env bash
set -euo pipefail
exec /usr/local/bin/rpm-repo-sync --repo citrix --keep 3 "$@"
