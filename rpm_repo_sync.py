#!/usr/bin/python3
"""Mirror upstream RPMs without altering them; retain three EVRs per name/arch."""
import argparse
import contextlib
import dataclasses
import email.message
import fcntl
import functools
import gzip
import hashlib
import json
import logging
import os
from pathlib import Path
import re
import shutil
import subprocess
import sys
import tempfile
import time
from urllib.parse import unquote, urljoin, urlsplit, urlunsplit
import xml.etree.ElementTree as ET

import requests
from bs4 import BeautifulSoup
import rpm
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

LOG = logging.getLogger("rpm-repo-sync")
APPS = ("bitwarden", "citrix", "rambox")
NAMES = {"bitwarden": {"bitwarden"}, "citrix": {"icaclient", "ctxusb", "ctxappprotection"},
         "rambox": {"rambox"}}
CITRIX_URL = "https://www.citrix.com/downloads/workspace-app/linux/workspace-app-for-linux-latest.html"
BITWARDEN_URL = "https://bitwarden.com/download/?app=desktop&platform=linux&variant=rpm"
NS = {"r": "http://linux.duke.edu/metadata/repo", "c": "http://linux.duke.edu/metadata/common"}
TIMEOUT = (15, 60)
MAX_DOWNLOAD = 1536 * 1024 * 1024


class SyncError(Exception):
    pass


@dataclasses.dataclass
class Asset:
    filename: str
    url: str
    names: set
    checksum: str | None = None
    size: int | None = None
    revision: str = ""

    @property
    def source_id(self):
        # Signed URLs expire daily; their query tokens are NOT asset identities.
        return json.dumps([self.filename, self.checksum, self.size, self.revision])


@dataclasses.dataclass
class Package:
    path: Path
    name: str
    arch: str
    evr: tuple
    sha256: str
    fresh: bool = False

    @property
    def public_path(self):
        # Use RPM headers rather than the source filename so migrations and
        # upstream filename changes cannot accumulate checksum decorations.
        epoch, version, release = self.evr
        epoch_prefix = epoch + "_" if epoch != "0" else ""
        filename = f"{self.name}-{epoch_prefix}{version}-{release}.{self.arch}-{self.sha256[:16]}.rpm"
        filename = re.sub(r"[^A-Za-z0-9._+\-]", "_", filename)
        return Path("Packages") / filename

    @property
    def identity(self):
        return (self.name, self.arch, *self.evr)


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


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


def discover_bitwarden(s):
    with s.get(BITWARDEN_URL, stream=True, timeout=TIMEOUT) as response:
        response.raise_for_status()
        cd = email.message.Message()
        cd["Content-Disposition"] = response.headers.get("Content-Disposition", "")
        filename = safe_filename(cd.get_filename() or Path(urlsplit(response.url).path).name)
        asset = Asset(filename, response.url, NAMES["bitwarden"],
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


def discover_rambox(s):
    api = "https://api.github.com/repos/ramboxapp/download/releases"
    latest = json_get(s, api + "/latest")
    def pick(release):
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
        a = entries[0]
        LOG.info("Rambox RPM release selected: %s", release.get("tag_name", "unknown"))
        return [Asset(safe_filename(a["name"]), a["browser_download_url"], NAMES["rambox"],
                      github_digest(a), a.get("size"), a.get("updated_at") or release.get("tag_name", ""))]
    selected = pick(latest)
    if selected:
        return selected
    LOG.info("Latest Rambox release has no compatible stable RPM; checking earlier stable releases")
    for page in range(1, 6):
        releases = json_get(s, api + f"?per_page=20&page={page}")
        if not isinstance(releases, list):
            raise SyncError("Unexpected Rambox releases API response")
        for release in releases:
            selected = pick(release)
            if selected:
                return selected
        if len(releases) < 20:
            break
    raise SyncError("No compatible stable Rambox RPM found in the latest 100 releases")


def discover_citrix(s):
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


def inspect_package(path, allowed_names, fresh=False):
    path = Path(path)
    if path.is_symlink() or not path.is_file():
        raise SyncError("RPM must be a regular file: " + str(path))
    with path.open("rb") as f:
        if f.read(4) != b"\xed\xab\xee\xdb":
            raise SyncError("Not an RPM package: " + str(path))
    # Check internal header/payload digests without requiring the author's key
    # in this mirror's RPM keyring. No signature or package bytes are changed.
    check = subprocess.run(["rpmkeys", "--define", "_pkgverify_level digest",
                            "--checksig", "--nosignature", str(path)],
                           capture_output=True, text=True, timeout=180)
    if check.returncode:
        raise SyncError("RPM integrity check failed: " + str(path))
    ts = rpm.TransactionSet()
    ts.setVSFlags(rpm._RPMVSF_NOSIGNATURES)
    with path.open("rb") as f:
        header = ts.hdrFromFdno(f.fileno())
    name, arch = str(header["name"]), str(header["arch"])
    if name.casefold() not in allowed_names or arch not in ("x86_64", "noarch"):
        raise SyncError(f"Unexpected package {name}.{arch}: {path.name}")
    evr = (str(header["epoch"] or 0), str(header["version"]), str(header["release"]))
    return Package(path, name, arch, evr, sha256(path), fresh)


def inventory(repo, app):
    paths = sorted(repo.glob("*.rpm")) + sorted((repo / "Packages").glob("*.rpm"))
    result = []
    for path in paths:
        try:
            result.append(inspect_package(path, NAMES[app]))
        except (SyncError, rpm.error) as exc:
            # Never silently delete or index a suspect package. It requires
            # explicit operator attention before this repository can update.
            raise SyncError(f"Existing RPM is invalid; move it out of the served directory and retry: {path.name}") from exc
    return result


def download(s, asset, work, existing, sources):
    if asset.checksum:
        for package in existing:
            if package.sha256 == asset.checksum and package.name.casefold() in asset.names:
                LOG.info("Already validated upstream RPM: %s", asset.filename)
                return dataclasses.replace(package, fresh=True)
    else:
        accepted = sources.get(asset.source_id)
        for package in existing:
            if accepted == package.sha256 and package.name.casefold() in asset.names:
                LOG.info("Already validated download: %s", asset.filename)
                return dataclasses.replace(package, fresh=True)
    path = work / asset.filename
    for attempt in range(1, 4):
        try:
            started = time.monotonic()
            LOG.info("Downloading %s from %s (attempt %d)", asset.filename, clean_url(asset.url), attempt)
            with s.get(asset.url, stream=True, timeout=TIMEOUT) as response:
                response.raise_for_status()
                length = int(response.headers.get("Content-Length") or 0)
                required = max(length, asset.size or 0)
                if required > MAX_DOWNLOAD:
                    raise SyncError("Download exceeds the 1.5 GiB per-file limit")
                if shutil.disk_usage(work).free < required + 128 * 1024 * 1024:
                    raise SyncError("Insufficient free disk space to stage the download")
                total = 0
                with path.open("wb") as f:
                    for chunk in response.iter_content(1024 * 1024):
                        if not chunk:
                            continue
                        total += len(chunk)
                        if total > MAX_DOWNLOAD or time.monotonic() - started > 1800:
                            raise SyncError("Download exceeded its size/time limit")
                        f.write(chunk)
                    f.flush()
                    os.fsync(f.fileno())
                if (length and total != length) or (asset.size and total != asset.size):
                    raise SyncError("Downloaded size does not match upstream size")
            package = inspect_package(path, asset.names, fresh=True)
            if asset.checksum and package.sha256 != asset.checksum:
                raise SyncError("Published SHA-256 does not match downloaded RPM")
            LOG.info("Validated %s (%d bytes, SHA-256 %s)", asset.filename, total, package.sha256)
            return package
        except (requests.RequestException, SyncError, OSError, rpm.error):
            path.unlink(missing_ok=True)
            if attempt == 3:
                raise
            LOG.warning("Download failed; retrying %s", asset.filename)
            time.sleep(attempt * 2)
    raise SyncError("Download exhausted its retries")


def select_packages(packages, keep):
    groups = {}
    for p in packages:
        groups.setdefault((p.name, p.arch), []).append(p)
    result = []
    def compare(a, b):
        version = rpm.labelCompare(a.evr, b.evr)
        if version:
            return -version
        # Prefer the newly validated upstream RPM over an old re-signed copy.
        if a.fresh != b.fresh:
            return -1 if a.fresh else 1
        return (str(a.public_path) > str(b.public_path)) - (str(a.public_path) < str(b.public_path))
    for key in sorted(groups):
        chosen = []
        for package in sorted(groups[key], key=functools.cmp_to_key(compare)):
            if any(rpm.labelCompare(package.evr, previous.evr) == 0 for previous in chosen):
                continue
            chosen.append(package)
            if len(chosen) == keep:
                break
        result.extend(chosen)
    return result


def metadata_refs(repomd):
    root = ET.parse(repomd).getroot()
    result = set()
    for node in root.findall("r:data/r:location", NS):
        href = node.get("href", "")
        p = Path(href)
        if p.parts[:1] != ("repodata",) or len(p.parts) != 2 or p.name in ("", ".", ".."):
            raise SyncError("Unexpected metadata location")
        result.add(href)
    return result


def validate_metadata(build, packages):
    root = ET.parse(build / "repodata/repomd.xml").getroot()
    primary = None
    refs = metadata_refs(build / "repodata/repomd.xml")
    for data in root.findall("r:data", NS):
        href = data.find("r:location", NS).get("href")
        checksum = data.find("r:checksum", NS)
        if checksum is None or checksum.get("type") != "sha256" or sha256(build / href) != checksum.text:
            raise SyncError("Generated metadata failed its checksum check")
        if data.get("type") == "primary":
            primary = build / href
    if primary is None or not refs:
        raise SyncError("Generated repository has no primary metadata")
    with gzip.open(primary, "rb") as f:
        entries = ET.parse(f).getroot().findall("c:package", NS)
    actual = {p.find("c:location", NS).get("href"): p.find("c:checksum", NS).text for p in entries}
    expected = {str(p.public_path): p.sha256 for p in packages}
    if actual != expected or len(entries) != len(packages):
        raise SyncError("Generated metadata does not exactly describe the retained RPMs")
    return refs


def link_or_copy(src, dest):
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    try:
        os.link(src, dest)
    except OSError:
        shutil.copyfile(src, dest)
    dest.chmod(0o644)


def atomic_copy(src, dest):
    dest.parent.mkdir(parents=True, exist_ok=True, mode=0o755)
    fd, temporary = tempfile.mkstemp(prefix=".publishing-", dir=dest.parent)
    try:
        with os.fdopen(fd, "wb") as target, Path(src).open("rb") as source:
            shutil.copyfileobj(source, target)
            target.flush()
            os.fsync(target.fileno())
        os.chmod(temporary, 0o644)
        os.replace(temporary, dest)
    finally:
        Path(temporary).unlink(missing_ok=True)


def atomic_json(path, value):
    fd, temporary = tempfile.mkstemp(prefix=".state-", dir=path.parent)
    try:
        with os.fdopen(fd, "w") as f:
            json.dump(value, f, indent=2, sort_keys=True)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary, path)
    finally:
        Path(temporary).unlink(missing_ok=True)


def publish(repo, build, kept, existing):
    current_refs = validate_metadata(build, kept)
    if (repo / "repodata/repomd.xml").exists():
        previous_refs = metadata_refs(repo / "repodata/repomd.xml")
    else:
        previous_refs = set()
    # All immutable RPMs and metadata objects arrive before repomd.xml switches.
    for package in kept:
        dest = repo / package.public_path
        if dest.exists() and sha256(dest) != package.sha256:
            raise SyncError("Existing package URL has different bytes; refusing to overwrite: " + str(dest))
        if not dest.exists():
            atomic_copy(build / package.public_path, dest)
    for href in sorted(current_refs):
        atomic_copy(build / href, repo / href)
    atomic_copy(build / "repodata/repomd.xml", repo / "repodata/repomd.xml")
    # repomd.xml is committed. Only now remove extra versions and legacy copies.
    retained = {repo / p.public_path for p in kept}
    for package in existing:
        if package.path not in retained:
            package.path.unlink(missing_ok=True)
            LOG.info("Removed old/duplicate RPM: %s", package.path.name)
    # Keep the current and immediately previous metadata generation. Metadata
    # is small; package retention remains exactly the requested three EVRs.
    for path in (repo / "repodata").iterdir():
        href = "repodata/" + path.name
        if path.is_file() and path.name != "repomd.xml" and href not in current_refs | previous_refs:
            path.unlink()
    return current_refs


@contextlib.contextmanager
def repo_lock(state):
    with (state / "update.lock").open("a") as f:
        try:
            fcntl.flock(f, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SyncError("An update of this repository is already running")
        yield


def sync(app, web_root, state_root, keep=3, dry_run=False):
    repo = web_root / app
    state = state_root / app
    repo.mkdir(parents=True, exist_ok=True, mode=0o755)
    state.mkdir(parents=True, exist_ok=True, mode=0o700)
    with repo_lock(state):
        state_file = state / "state.json"
        try:
            previous = json.loads(state_file.read_text()) if state_file.exists() else {}
        except (ValueError, OSError):
            raise SyncError("Updater state is unreadable: " + str(state_file))
        # Work outside the web root, on the same filesystem where practical.
        work_root = web_root.parent / ".rpm-repo-sync-work"
        work_root.mkdir(parents=True, exist_ok=True, mode=0o700)
        # The repo lock proves no other new job for this app is using these.
        # Clean staging left behind by a killed process or container restart.
        for stale in work_root.glob(app + "-*"):
            if stale.is_dir() and not stale.is_symlink():
                shutil.rmtree(stale)
        with tempfile.TemporaryDirectory(prefix=app + "-", dir=work_root) as directory, session() as s:
            work = Path(directory)
            existing = inventory(repo, app)
            assets = {"bitwarden": discover_bitwarden, "citrix": discover_citrix,
                      "rambox": discover_rambox}[app](s)
            fresh = [download(s, asset, work, existing, previous.get("sources", {})) for asset in assets]
            kept = select_packages(existing + fresh, keep)
            for p in kept:
                LOG.info("Keep %s.%s %s:%s-%s", p.name, p.arch, *p.evr)
            if dry_run:
                LOG.info("Dry run: validated downloads and retention; nothing published or removed")
                return
            plan = sorted(str(p.public_path) for p in kept)
            sources = {asset.source_id: p.sha256 for asset, p in zip(assets, fresh)
                       if p.sha256 in {k.sha256 for k in kept}}
            repomd = repo / "repodata/repomd.xml"
            extras = any(p.path != repo / p.public_path or str(p.public_path) not in plan for p in existing)
            unchanged = (plan == previous.get("packages") and not extras and repomd.exists()
                         and sha256(repomd) == previous.get("repomd_sha256"))
            if unchanged:
                if sources != previous.get("sources"):
                    previous["sources"] = sources
                    atomic_json(state_file, previous)
                LOG.info("Package set unchanged; metadata rebuild skipped")
                return
            build = work / "build"
            build.mkdir()
            for package in kept:
                link_or_copy(package.path, build / package.public_path)
            subprocess.run(["createrepo_c", "--no-database", "--unique-md-filenames",
                            "--checksum", "sha256", "--general-compress-type", "gz", str(build)],
                           check=True, timeout=900)
            publish(repo, build, kept, existing)
            atomic_json(state_file, {"packages": plan, "sources": sources,
                                    "repomd_sha256": sha256(repomd)})
            LOG.info("Published %s: %d RPMs, at most %d versions per package/architecture", app, len(kept), keep)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--repo", choices=(*APPS, "all"), default="all")
    parser.add_argument("--web-root", type=Path, default=Path("/var/www/html"))
    parser.add_argument("--state-dir", type=Path, default=Path("/var/lib/rpm-repo-sync"))
    parser.add_argument("--log-dir", type=Path, default=Path("/var/log/rpm-repo-sync"))
    parser.add_argument("--keep", type=int, default=3)
    parser.add_argument("--dry-run", action="store_true", help="Download/validate in staging, but do not publish or prune")
    args = parser.parse_args()
    if args.keep < 1:
        parser.error("--keep must be at least 1")
    for command in ("rpmkeys", "createrepo_c"):
        if not shutil.which(command):
            parser.error("Missing command: " + command)
    formatter = logging.Formatter("%(asctime)s UTC %(levelname)s %(message)s")
    formatter.converter = time.gmtime
    LOG.setLevel(logging.INFO)
    console = logging.StreamHandler(sys.stdout)
    console.setFormatter(formatter)
    LOG.addHandler(console)
    args.log_dir.mkdir(parents=True, exist_ok=True, mode=0o755)
    failed = False
    for app in APPS if args.repo == "all" else (args.repo,):
        file_handler = logging.FileHandler(args.log_dir / (app + ".log"))
        file_handler.setFormatter(formatter)
        LOG.addHandler(file_handler)
        try:
            LOG.info("Starting %s repository update", app)
            sync(app, args.web_root, args.state_dir, args.keep, args.dry_run)
            LOG.info("Completed %s repository update", app)
        except Exception as exc:
            failed = True
            # Requests exceptions may contain signed query tokens: omit those.
            message = re.sub(r"https?://[^\s]+", lambda m: clean_url(m[0]), str(exc))
            LOG.error("%s update failed: %s", app, message)
        finally:
            LOG.removeHandler(file_handler)
            file_handler.close()
    return int(failed)


if __name__ == "__main__":
    sys.exit(main())
