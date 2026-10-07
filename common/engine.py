"""Validate, retain and publish RPMs for any registered service."""
import contextlib
import dataclasses
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
import tempfile
import time
import xml.etree.ElementTree as ET
import requests
import rpm
from common.models import Asset, Package, SyncError
from common.http import TIMEOUT, clean_url, safe_filename, session

LOG = logging.getLogger("rpm-repo-sync")
NS = {"r": "http://linux.duke.edu/metadata/repo", "c": "http://linux.duke.edu/metadata/common"}
MAX_DOWNLOAD = 1536 * 1024 * 1024


def sha256(path):
    h = hashlib.sha256()
    with Path(path).open("rb") as f:
        for chunk in iter(lambda: f.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


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


def inventory(repo, package_names):
    paths = sorted(repo.glob("*.rpm")) + sorted((repo / "Packages").glob("*.rpm"))
    result = []
    for path in paths:
        try:
            result.append(inspect_package(path, package_names))
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


def sync(service, web_root, state_root, keep=3, dry_run=False):
    app = service.config.id
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
            existing = inventory(repo, service.config.package_names)
            assets = service.discover(s)
            if not isinstance(assets, list) or not assets:
                raise SyncError("Service discovery must return a non-empty list of Asset objects")
            for asset in assets:
                if not isinstance(asset, Asset) or not asset.names or not asset.names <= service.config.package_names:
                    raise SyncError("Discovered package names do not match the service configuration")
                safe_filename(asset.filename)
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
