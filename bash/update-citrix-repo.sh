#!/usr/bin/env bash
set -euo pipefail

# Repo directory served by Apache
REPO_DIR="/var/www/html/citrix"

# Path to Python downloader script
PY_SCRIPT="/root/citrix/citrix_downloader.py"

# Log file
LOG_FILE="/root/citrix/citrix-repo-update.log"

{
  echo "===== $(date -u '+%Y-%m-%d %H:%M:%S UTC') Starting Citrix repo update ====="

  # 1) Download latest RPMs
  python3 "$PY_SCRIPT" --out-dir "$REPO_DIR"

  # 2) Keep only the last 3 versions of each package (by RPM name)
  keep_last_n() {
    local pattern="$1"
    local n="$2"

    # If fewer than (n+1) files exist, tail will output nothing => xargs -r does nothing.
    ls -t ${pattern} 2>/dev/null | tail -n +"$((n + 1))" | xargs -r rm -f
  }

  keep_last_n "$REPO_DIR/ICAClient-rhel-*.rpm" 3
  keep_last_n "$REPO_DIR/ctxusb-*.rpm" 3
  keep_last_n "$REPO_DIR/ctxappprotection-*.rpm" 3

  # sign packages rpm
  --resign $REPO_DIR/*.rpm

  # 3) Update repository metadata
  createrepo --update "$REPO_DIR"

  echo "===== $(date -u '+%Y-%m-%d %H:%M:%S UTC') Done ====="
  echo
} >> "$LOG_FILE" 2>&1