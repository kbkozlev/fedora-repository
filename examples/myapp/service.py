"""Starter for a vendor with a fixed latest-RPM URL. Configure before installing."""
from common.models import Asset, SyncError
from common.http import TIMEOUT, safe_filename
import email.message
from pathlib import Path
import time
from urllib.parse import urlsplit

RPM_URL = "https://example.invalid/download/myapp-latest.rpm"


def discover(session, config):
    if "example.invalid" in RPM_URL:
        raise SyncError("Set this service's official vendor RPM URL before installing it")
    # Resolve headers without consuming the payload. Versioned filenames,
    # published checksums and stable upstream revisions avoid repeat downloads.
    with session.get(RPM_URL, stream=True, timeout=TIMEOUT) as response:
        response.raise_for_status()
        disposition = email.message.Message()
        disposition["Content-Disposition"] = response.headers.get("Content-Disposition", "")
        filename = safe_filename(disposition.get_filename() or Path(urlsplit(response.url).path).name)
        revision = response.headers.get("ETag") or response.headers.get("Last-Modified")
        # A fixed 'latest' URL with no revision must be fetched each run, otherwise
        # a new version could be missed forever. Full bytes are validated by the engine.
        revision = revision or str(time.time_ns())
        size = int(response.headers.get("Content-Length") or 0) or None
        return [Asset(filename, response.url, config.package_names, size=size, revision=revision)]
