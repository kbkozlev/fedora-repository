"""Official bitwarden RPM discovery; publication belongs to the common engine."""
import logging
from pathlib import Path
import re
import requests
from common.models import Asset, SyncError
from common.http import TIMEOUT, safe_filename, json_get, github_digest

LOG = logging.getLogger("rpm-repo-sync")
import email.message
from urllib.parse import urlsplit
BITWARDEN_URL = "https://bitwarden.com/download/?app=desktop&platform=linux&variant=rpm"


def discover(s, config):
    with s.get(BITWARDEN_URL, stream=True, timeout=TIMEOUT) as response:
        response.raise_for_status()
        cd = email.message.Message()
        cd["Content-Disposition"] = response.headers.get("Content-Disposition", "")
        filename = safe_filename(cd.get_filename() or Path(urlsplit(response.url).path).name)
        asset = Asset(filename, response.url, config.package_names,
                      size=int(response.headers.get("Content-Length") or 0) or None,
                      revision=response.headers.get("ETag", ""))
    # GitHub provides an additional digest on many releases. Its availability
    # is optional: the official HTTPS RPM and internal RPM digests still work.
    match = re.search(r"Bitwarden-([0-9]+\.[0-9]+\.[0-9]+)", filename, re.I)
    if match:
        try:
            release = json_get(s, "https://api.github.com/repos/bitwarden/clients/releases/tags/desktop-v" + match[1])
            for entry in release.get("assets", []):
                if entry.get("name") == filename:
                    asset.checksum = github_digest(entry)
                    asset.size = entry.get("size") or asset.size
                    asset.revision = entry.get("updated_at") or asset.revision
                    break
        except (requests.RequestException, ValueError):
            LOG.info("Bitwarden release digest unavailable; using official HTTPS download and RPM integrity checks")
    return [asset]
