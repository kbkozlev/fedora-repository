#!/usr/bin/env bash
set -euo pipefail

usage() {
  echo 'Usage: bash install.sh SERVICE [SERVICE ...] [--no-sync] [--timezone ZONE]'
  echo '       bash install.sh --all [--no-sync] [--timezone ZONE]'
  echo 'Each services/<id>/install.sh installs only that service.'
  echo 'The existing server timezone is preserved unless --timezone is provided.'
}
selected=()
run_sync=1
timezone=""
while (( $# )); do
  case "$1" in
    --help|-h) usage; exit 0 ;;
    --all) selected+=(all); shift ;;
    --no-sync) run_sync=0; shift ;;
    --timezone) if (( $# < 2 )) || [[ -z $2 ]]; then usage >&2; exit 2; fi; timezone=$2; shift 2 ;;
    --*) echo "Unknown option: $1" >&2; usage >&2; exit 2 ;;
    *) selected+=("$1"); shift ;;
  esac
done
if (( ${#selected[@]} == 0 )); then usage >&2; exit 2; fi
if [[ ${EUID} -ne 0 ]]; then echo 'Run as root inside the Fedora repository server.' >&2; exit 1; fi
source /etc/os-release
if [[ ${ID} != fedora ]]; then echo 'This installer targets Fedora.' >&2; exit 1; fi
if [[ -n $timezone && ! $timezone =~ ^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$ ]]; then
  echo 'Invalid timezone name.' >&2; exit 2
fi

bundle_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
installed_dir=/usr/local/lib/rpm-repo-sync
if ! command -v /usr/bin/python3 flock pgrep > /dev/null; then
  dnf -y install python3 util-linux procps-ng
fi
install -d -m 0700 /var/lib/rpm-repo-sync
exec 9>/var/lib/rpm-repo-sync/install.lock
if ! flock -n 9; then
  echo 'A repository update or installation is running. Let it finish, then retry.' >&2; exit 1
fi
# The former bundled updater does not take the shared installation lock.
if pgrep -f '^(/bin/)?bash /root/[^/]+/update-.*-repo.sh' > /dev/null \
   || pgrep -f '^python3 /root/[^/]+/.*_downloader.py' > /dev/null \
   || pgrep -f '^/usr/bin/python3 /usr/local/lib/rpm-repo-sync/rpm_repo_sync.py' > /dev/null; then
  echo 'A repository process is active. Let it finish, then retry.' >&2; exit 1
fi
backup_dir="/root/repo-updater-backup-$(date -u +%Y%m%dT%H%M%SZ)-$$"
install -d -m 0700 "$backup_dir"
crontab -l > "$backup_dir/root.crontab" 2>/dev/null || true
if [[ -d $installed_dir ]]; then cp -a "$installed_dir" "$backup_dir/installed-updater"; fi
if [[ -f /usr/local/bin/rpm-repo-sync ]]; then cp -a /usr/local/bin/rpm-repo-sync "$backup_dir/launcher"; fi
if [[ -n $timezone && -e /etc/localtime ]]; then cp -a /etc/localtime "$backup_dir/localtime"; fi
for path in /etc/httpd/conf.d/rpm-repo-sync.conf /etc/logrotate.d/rpm-repo-sync; do
  if [[ -f $path ]]; then cp -a "$path" "$backup_dir/$(basename "$(dirname "$path")")-$(basename "$path")"; fi
done

# Read manifests using the standard library; unrelated vendor modules are not imported.
/usr/bin/python3 "$bundle_dir/common/install_config.py" plan \
  --source "$bundle_dir/services" --installed "$installed_dir" "${selected[@]}" > "$backup_dir/plan.json"
/usr/bin/python3 - "$backup_dir/plan.json" <<'PY'
import json,sys
from pathlib import Path
p=json.loads(Path(sys.argv[1]).read_text())
print('Requested services: '+', '.join(p['selected']))
if p['migrated']: print('Preserving existing bundled services during migration: '+', '.join(p['migrated']))
for key in ('selected','deploy','dependencies'):
    Path(sys.argv[1]).with_name(key+'.txt').write_text(''.join(x+'\n' for x in p[key]))
PY
mapfile -t deploy < "$backup_dir/deploy.txt"
mapfile -t selected < "$backup_dir/selected.txt"
mapfile -t extra_dependencies < "$backup_dir/dependencies.txt"
if [[ -n $timezone ]]; then extra_dependencies+=(tzdata); fi
for app in "${deploy[@]}"; do
  test -f "$bundle_dir/services/$app/update.sh"
  if [[ -f /root/$app/update-$app-repo.sh ]]; then
    cp -a "/root/$app/update-$app-repo.sh" "$backup_dir/update-$app-repo.sh"
  fi
done

echo "Installing shared dependencies and selected service requirements; backup: $backup_dir"
dnf -y install python3 python3-requests python3-rpm rpm createrepo_c cronie \
  logrotate procps-ng util-linux httpd "${extra_dependencies[@]}"
command -v rpmkeys createrepo_c > /dev/null
/usr/bin/python3 -m compileall -q "$bundle_dir/common" "$bundle_dir/services" "$bundle_dir/rpm_repo_sync.py"
PYTHONPATH="$bundle_dir" /usr/bin/python3 - "$bundle_dir/services" "${deploy[@]}" <<'PY'
import sys
from common.registry import load_service
for name in sys.argv[2:]: load_service(sys.argv[1],name)
PY

source "$bundle_dir/common/timezone.sh"
configure_timezone "$timezone"
echo "Server local time: $(date '+%Y-%m-%d %H:%M:%S %Z %z')"
install -d -m 0755 "$installed_dir" /var/log/rpm-repo-sync
install -d -m 0700 /var/www/.rpm-repo-sync-work
# Replace each code file atomically while the installation lock is held.
/usr/bin/python3 "$bundle_dir/common/install_config.py" deploy \
  --source "$bundle_dir" --installed "$installed_dir" "${deploy[@]}"
cat > "$backup_dir/launcher.new" <<'LAUNCHER'
#!/usr/bin/env bash
exec /usr/bin/python3 /usr/local/lib/rpm-repo-sync/rpm_repo_sync.py "$@"
LAUNCHER
install -m 0755 "$backup_dir/launcher.new" /usr/local/bin/.rpm-repo-sync.new
mv -f /usr/local/bin/.rpm-repo-sync.new /usr/local/bin/rpm-repo-sync
for app in "${deploy[@]}"; do
  install -d -m 0700 "/root/$app"
  install -m 0755 "$bundle_dir/services/$app/update.sh" "/root/$app/.update-$app-repo.sh.new"
  mv -f "/root/$app/.update-$app-repo.sh.new" "/root/$app/update-$app-repo.sh"
done

cat > /etc/httpd/conf.d/rpm-repo-sync.conf <<'APACHE'
<IfModule autoindex_module>
    <Directory "/var/www/html">
        IndexOptions FancyIndexing HTMLTable VersionSort NameWidth=*
    </Directory>
</IfModule>
<IfModule headers_module>
    <Directory "/var/www/html">
        <FilesMatch "^(repomd\.xml|.*\.repo)$">
            Header always set Cache-Control "no-store"
        </FilesMatch>
    </Directory>
</IfModule>
APACHE
if ! httpd -t; then
  if [[ -f $backup_dir/conf.d-rpm-repo-sync.conf ]]; then
    cp -a "$backup_dir/conf.d-rpm-repo-sync.conf" /etc/httpd/conf.d/rpm-repo-sync.conf
  else
    mv /etc/httpd/conf.d/rpm-repo-sync.conf "$backup_dir/rejected-apache.conf"
  fi
  echo 'Apache validation failed; its prior include was restored. Cron was not changed.' >&2; exit 1
fi
if systemctl is-active --quiet httpd; then systemctl restart httpd; fi
cat > /etc/logrotate.d/rpm-repo-sync <<'LOGROTATE'
/var/log/rpm-repo-sync/*.log {
    daily
    rotate 7
    compress
    delaycompress
    missingok
    notifempty
    copytruncate
    su root root
}
LOGROTATE
/usr/bin/python3 "$bundle_dir/common/install_config.py" cron "$backup_dir/plan.json" \
  "$backup_dir/root.crontab" > "$backup_dir/new.crontab"
crontab "$backup_dir/new.crontab"
systemctl enable --now crond
systemctl restart crond
flock -u 9
exec 9>&-
echo "Installed selected services. Configuration/code backup: $backup_dir"
if (( run_sync )); then
  failed=0
  for app in "${selected[@]}"; do
    /usr/local/bin/rpm-repo-sync --repo "$app" --keep 3 || failed=1
  done
  if (( failed )); then
    echo 'A selected sync failed. Check /var/log/rpm-repo-sync/*.log; existing data is preserved until publication validates.' >&2
    exit 1
  fi
else
  echo 'Initial sync skipped. Run rpm-repo-sync --repo SERVICE when ready.'
fi
