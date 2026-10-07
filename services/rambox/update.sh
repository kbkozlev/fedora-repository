#!/usr/bin/env bash
set -euo pipefail
exec /usr/local/bin/rpm-repo-sync --repo rambox --keep 3 "$@"
