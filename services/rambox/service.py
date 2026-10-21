"""Official Rambox RPM discovery; publication belongs to the common engine."""
import logging
import re
from urllib.parse import quote
from common.models import Asset, SyncError
from common.http import safe_filename, json_get, github_digest

LOG = logging.getLogger("rpm-repo-sync")
API = "https://api.github.com/repos/ramboxapp/download/releases"


def pick_release(release, config):
    if release.get("draft") or release.get("prerelease"):
        return None
    rpms = [a for a in release.get("assets", []) if a.get("name", "").lower().endswith(".rpm")]
    entries = [a for a in rpms if re.search(r"(?:x64|x86_64|amd64).*\.rpm$", a["name"], re.I)]
    if not entries:
        # A generic single RPM can also be used: its real arch is checked
        # from the RPM header before publishing, never from filenames alone.
        entries = [a for a in rpms if not re.search(r"arm|aarch|i[3-6]86|\.src\.rpm$", a["name"], re.I)]
    if not entries:
        return None
    if len(entries) != 1:
        raise SyncError("Ambiguous Rambox RPM assets in release " + release.get("tag_name", "unknown"))
    asset = entries[0]
    LOG.info("Rambox RPM release selected: %s", release.get("tag_name", "unknown"))
    return [Asset(safe_filename(asset["name"]), asset["browser_download_url"], config.package_names,
                  github_digest(asset), asset.get("size"), asset.get("updated_at") or release.get("tag_name", ""))]


def discover_release(s, config, tag):
    """Exact, one-run selection: never fall back to another release."""
    release = json_get(s, API + "/tags/" + quote(tag, safe=""))
    if release.get("tag_name") != tag:
        raise SyncError("Rambox API did not return the requested release: " + tag)
    if release.get("draft") or release.get("prerelease"):
        raise SyncError("Requested Rambox release is a draft or prerelease: " + tag)
    selected = pick_release(release, config)
    if not selected:
        raise SyncError("Requested Rambox release has no compatible RPM: " + tag)
    return selected


def discover(s, config):
    latest = json_get(s, API + "/latest")
    selected = pick_release(latest, config)
    if selected:
        return selected
    LOG.info("Latest Rambox release has no compatible stable RPM; checking earlier stable releases")
    for page in range(1, 6):
        releases = json_get(s, API + f"?per_page=20&page={page}")
        if not isinstance(releases, list):
            raise SyncError("Unexpected Rambox releases API response")
        for release in releases:
            selected = pick_release(release, config)
            if selected:
                return selected
        if len(releases) < 20:
            break
    raise SyncError("No compatible stable Rambox RPM found in the latest 100 releases")
