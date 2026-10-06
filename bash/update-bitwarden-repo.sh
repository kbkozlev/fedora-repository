#!/usr/bin/env bash
set -euo pipefail
exec /usr/local/bin/rpm-repo-sync --repo bitwarden --keep 3
