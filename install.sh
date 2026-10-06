#!/usr/bin/env bash
set -euo pipefail

if [[ ${EUID} -ne 0 ]]; then
  echo 'Run this installer as root INSIDE the Fedora repository LXC.' >&2
  exit 1
fi
source /etc/os-release
if [[ ${ID} != fedora ]]; then
  echo 'This installer targets your Fedora LXC.' >&2
  exit 1
fi

bundle_dir=$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)
backup_dir="/root/repo-updater-backup-$(date -u +%Y%m%dT%H%M%SZ)"
install -d -m 0700 "$backup_dir"
crontab -l > "$backup_dir/root.crontab" 2>/dev/null || true
for app in bitwarden citrix rambox; do
  if [[ -f "/root/$app/update-$app-repo.sh" ]]; then
    cp -a "/root/$app/update-$app-repo.sh" "$backup_dir/update-$app-repo.sh"
  fi
done
for name in rpm-repo-sync.conf; do
  if [[ -f "/etc/httpd/conf.d/$name" ]]; then
    cp -a "/etc/httpd/conf.d/$name" "$backup_dir/$name"
  fi
done
if [[ -f /usr/local/lib/rpm-repo-sync/rpm_repo_sync.py ]]; then
  cp -a /usr/local/lib/rpm-repo-sync/rpm_repo_sync.py "$backup_dir/rpm_repo_sync.py"
fi
if [[ -e /etc/localtime ]]; then
  cp -a /etc/localtime "$backup_dir/localtime"
fi

echo "Installing runtime dependencies; backup: $backup_dir"
dnf -y install python3 python3-requests python3-beautifulsoup4 python3-rpm \
  rpm createrepo_c cronie logrotate procps-ng tzdata
/usr/bin/python3 -c 'import requests, bs4, rpm'
command -v rpmkeys createrepo_c > /dev/null
/usr/bin/python3 -m py_compile "$bundle_dir/rpm_repo_sync.py"

# Set the LXC's display/scheduling timezone, retaining the shared host clock.
# Some minimal containers lack a working timedated/DBus service.
if ! timedatectl set-timezone Europe/Sofia; then
  test -f /usr/share/zoneinfo/Europe/Sofia
  ln -sfn /usr/share/zoneinfo/Europe/Sofia /etc/localtime
fi
echo "Container local time: $(date '+%Y-%m-%d %H:%M:%S %Z %z')"

# Refuse to replace a wrapper while its old downloader/maintenance job is active.
if pgrep -f '^(/bin/)?bash /root/(bitwarden|citrix|rambox)/update-.*-repo.sh' > /dev/null \
   || pgrep -f '^python3 /root/(bitwarden|citrix|rambox)/.*_downloader.py' > /dev/null; then
  echo 'An old repository job is running. Let it finish, then rerun this installer.' >&2
  exit 1
fi

install -d -m 0755 /usr/local/lib/rpm-repo-sync /var/log/rpm-repo-sync
install -d -m 0700 /var/lib/rpm-repo-sync /var/www/.rpm-repo-sync-work
install -m 0644 "$bundle_dir/rpm_repo_sync.py" /usr/local/lib/rpm-repo-sync/rpm_repo_sync.py
cat > /usr/local/bin/rpm-repo-sync <<'EOF'
#!/usr/bin/env bash
exec /usr/bin/python3 /usr/local/lib/rpm-repo-sync/rpm_repo_sync.py "$@"
EOF
chmod 0755 /usr/local/bin/rpm-repo-sync

for app in bitwarden citrix rambox; do
  install -d -m 0700 "/root/$app"
  cat > "/root/$app/update-$app-repo.sh" <<EOF
#!/usr/bin/env bash
set -euo pipefail
exec /usr/local/bin/rpm-repo-sync --repo $app --keep 3
EOF
  chmod 0755 "/root/$app/update-$app-repo.sh"
done

# Leave the original virtual host and repo definitions intact. The small
# include displays complete filenames and avoids caching the mutable entry point.
cat > /etc/httpd/conf.d/rpm-repo-sync.conf <<'EOF'
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
EOF
if ! httpd -t; then
  if [[ -f "$backup_dir/rpm-repo-sync.conf" ]]; then
    cp -a "$backup_dir/rpm-repo-sync.conf" /etc/httpd/conf.d/rpm-repo-sync.conf
  else
    rm -f /etc/httpd/conf.d/rpm-repo-sync.conf
  fi
  echo 'Apache configuration check failed; cache include restored. Original cron is still active.' >&2
  exit 1
fi
if systemctl is-active --quiet httpd; then
  systemctl restart httpd
fi

cat > /etc/logrotate.d/rpm-repo-sync <<'EOF'
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
EOF

# Preserve unrelated cron jobs. Spread these three downloads over 20 minutes.
awk '!/\/root\/(bitwarden|citrix|rambox)\/update-(bitwarden|citrix|rambox)-repo\.sh/' \
  "$backup_dir/root.crontab" > "$backup_dir/new.crontab"
cat >> "$backup_dir/new.crontab" <<'EOF'
0 0 * * * /bin/bash /root/bitwarden/update-bitwarden-repo.sh >> /var/log/rpm-repo-sync/cron.log 2>&1
10 0 * * * /bin/bash /root/citrix/update-citrix-repo.sh >> /var/log/rpm-repo-sync/cron.log 2>&1
20 0 * * * /bin/bash /root/rambox/update-rambox-repo.sh >> /var/log/rpm-repo-sync/cron.log 2>&1
EOF
crontab "$backup_dir/new.crontab"
systemctl enable --now crond
systemctl restart crond

echo 'Installed. Running the first sync for all three repositories.'
echo 'Existing packages stay available until each replacement repository validates.'
if /usr/local/bin/rpm-repo-sync --repo all --keep 3; then
  echo "First sync completed. Configuration/script backup: $backup_dir"
else
  echo 'One or more repositories did not update. Check /var/log/rpm-repo-sync/*.log and retry:' >&2
  echo '  rpm-repo-sync --repo all' >&2
  exit 1
fi
