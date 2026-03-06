#!/usr/bin/env bash
set -euo pipefail

# Repo directory served by Apache
REPO_DIR="/var/www/html/bitwarden"

# Path to Python downloader script
PY_SCRIPT="/root/bitwarden/bitwarden_downloader.py"

# Log file
LOG_FILE="/root/bitwarden/bitwarden-repo-update.log"

{
  echo "===== $(date -u '+%Y-%m-%d %H:%M:%S UTC') Starting Bitwarden repo update ====="

  # 1) Download latest RPMs
  python3 "$PY_SCRIPT" --out-dir "$REPO_DIR"

  # 2) Keep only the last 3 versions of each package (by RPM name)
  keep_last_n() {
    local pattern="$1"
    local n="$2"

    # If fewer than (n+1) files exist, tail will output nothing => xargs -r does nothing.
    ls -t ${pattern} 2>/dev/null | tail -n +"$((n + 1))" | xargs -r rm -f
  }

  keep_last_n "$REPO_DIR/Bitwarden-*.rpm" 3

  # sign packages rpm
  --resign $REPO_DIR/*.rpm

  # 3) Update repository metadata
  createrepo --update "$REPO_DIR"

  echo "===== $(date -u '+%Y-%m-%d %H:%M:%S UTC') Done ====="
  echo
} >> "$LOG_FILE" 2>&1