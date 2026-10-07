"""Official citrix RPM discovery; publication belongs to the common engine."""
import logging
from pathlib import Path
import re
import requests
from common.models import Asset, SyncError
from common.http import TIMEOUT, safe_filename, json_get, github_digest

LOG = logging.getLogger("rpm-repo-sync")
from bs4 import BeautifulSoup
from urllib.parse import urljoin, urlsplit
CITRIX_URL = "https://www.citrix.com/downloads/workspace-app/linux/workspace-app-for-linux-latest.html"


def discover(s, config):
    with s.get(CITRIX_URL, timeout=TIMEOUT) as response:
        response.raise_for_status()
        soup = BeautifulSoup(response.text, "html.parser")
    assets = []
    for heading, name in [("RedHat Full Package", "icaclient"),
                          ("USB Support Package", "ctxusb"),
                          ("App Protection Package", "ctxappprotection")]:
        matches = []
        for container in soup.select("div.ctx-dl-details"):
            h = container.find("h4")
            if not h or heading not in h.get_text(" ", strip=True):
                continue
            for anchor in container.select("a.ctx-dl-link"):
                # rel may be a token list in BeautifulSoup. Check each value,
                # then href, rather than joining unrelated values into a URL.
                rel = anchor.get("rel") or []
                values = (rel if isinstance(rel, list) else [rel]) + [anchor.get("href") or ""]
                for raw in values:
                    url = urljoin(CITRIX_URL, raw.strip())
                    parsed = urlsplit(url)
                    if parsed.scheme == "https" and parsed.path.lower().endswith(".rpm"):
                        digest = re.search(r"SHA-256\s*[-:]\s*([0-9a-fA-F]{64})",
                                           container.get_text(" ", strip=True))
                        if not digest:
                            raise SyncError("Citrix changed its download page: published RPM checksum missing")
                        matches.append(Asset(safe_filename(Path(parsed.path).name), url, {name}, digest[1].lower()))
                        break
        unique = {a.filename: a for a in matches}
        if len(unique) != 1:
            raise SyncError("Citrix changed its download page: expected one RPM for " + heading)
        assets.append(next(iter(unique.values())))
    return assets
