#!/usr/bin/env bash
# Installer helper. With no timezone option, leave the existing configuration alone.
configure_timezone() {
  local requested_zone="${1:-}"
  if [[ -z $requested_zone ]]; then return 0; fi
  if [[ ! $requested_zone =~ ^[A-Za-z0-9_+-]+(/[A-Za-z0-9_+-]+)*$ ]] \
     || [[ ! -f /usr/share/zoneinfo/$requested_zone ]]; then
    echo "Invalid or unavailable timezone: $requested_zone" >&2
    return 2
  fi
  if ! timedatectl set-timezone "$requested_zone"; then
    # Minimal containers may not run timedated/DBus.
    ln -sfn "/usr/share/zoneinfo/$requested_zone" /etc/localtime
  fi
}
