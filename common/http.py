"""Shared HTTP helpers; no application-specific URLs."""
import logging
from pathlib import Path
import re
from urllib.parse import unquote, urlsplit, urlunsplit
import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from common.models import SyncError

LOG = logging.getLogger("rpm-repo-sync")
TIMEOUT = (15, 60)


def safe_filename(value):
    value = unquote(value)
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._+\-]*\.rpm", value):
        raise SyncError("Unexpected RPM filename received from upstream")
    return value


def clean_url(value):
    p = urlsplit(value)
    return urlunsplit((p.scheme, p.netloc, p.path, "", ""))


def session():
    s = requests.Session()
    s.headers.update({"User-Agent": "Kozlev-rpm-mirror/2.0", "Accept-Encoding": "identity"})
    retry = Retry(total=3, backoff_factor=1, status_forcelist=(429, 500, 502, 503, 504),
                  allowed_methods=("GET", "HEAD"), respect_retry_after_header=True)
    s.mount("https://", HTTPAdapter(max_retries=retry))
    return s


def json_get(s, url):
    with s.get(url, timeout=TIMEOUT, headers={"Accept": "application/vnd.github+json"}) as response:
        response.raise_for_status()
        return response.json()


def github_digest(asset):
    digest = asset.get("digest") or ""
    if re.fullmatch(r"sha256:[0-9a-fA-F]{64}", digest):
        return digest[7:].lower()
    return None
