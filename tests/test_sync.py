"""Retention tests use librpm; integration tests build actual small RPMs."""
import contextlib
import dataclasses
import hashlib
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import importlib.util
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from unittest import mock

import requests
import rpm

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import engine as mirror
from common.registry import load_service
BITWARDEN = load_service(ROOT / "services", "bitwarden")
CITRIX = load_service(ROOT / "services", "citrix")
RAMBOX = load_service(ROOT / "services", "rambox")


def p(version, epoch="0", release="1", arch="x86_64", name="bitwarden", fresh=False):
    return mirror.Package(Path(f"{name}-{version}-{release}.{arch}.rpm"), name, arch,
                          (epoch, version, release), version.replace(".", "0").ljust(64, "0")[:64], fresh)


class RetentionTests(unittest.TestCase):
    def test_numeric_versions(self):
        selected = mirror.select_packages([p(v) for v in ("1.9", "1.10", "1.11", "1.2")], 3)
        self.assertEqual([x.evr[1] for x in selected], ["1.11", "1.10", "1.9"])

    def test_epoch_overrides_version(self):
        selected = mirror.select_packages([p("99"), p("1", epoch="1"), p("2")], 1)
        self.assertEqual(selected[0].evr, ("1", "1", "1"))

    def test_releases_count_as_distinct_versions(self):
        selected = mirror.select_packages([p("1", release=v) for v in ("1", "2", "10", "3")], 3)
        self.assertEqual([x.evr[2] for x in selected], ["10", "3", "2"])

    def test_prerelease_order(self):
        selected = mirror.select_packages([p(v) for v in ("1.0~rc1", "1.0", "1.0^git1", "1.1")], 3)
        self.assertEqual([x.evr[1] for x in selected], ["1.1", "1.0^git1", "1.0"])

    def test_each_architecture_and_package_keeps_three(self):
        packages = [p(str(i), name=n, arch=a) for n in ("ICAClient", "ctxusb", "ctxappprotection")
                    for a in ("x86_64", "noarch") for i in range(1, 6)]
        chosen = mirror.select_packages(packages, 3)
        self.assertEqual(len(chosen), 18)
        for n in ("ICAClient", "ctxusb", "ctxappprotection"):
            for a in ("x86_64", "noarch"):
                self.assertEqual([x.evr[1] for x in chosen if x.name == n and x.arch == a], ["5", "4", "3"])

    def test_original_upstream_copy_wins_duplicate_version(self):
        old = p("1")
        fresh = dataclasses.replace(old, fresh=True, sha256="f" * 64)
        self.assertEqual(mirror.select_packages([old, fresh], 3), [fresh])

    def test_filename_change_does_not_change_retention(self):
        old = dataclasses.replace(p("26.01.0.150", name="ICAClient"), path=Path("ICAClient-rhel-old.rpm"))
        new = dataclasses.replace(p("26.04.10.1", name="ICAClient"), path=Path("ICAClient-rhel-gcc-8-new.rpm"))
        self.assertEqual(mirror.select_packages([old, new], 1), [new])

    def test_readable_paths_depend_on_headers_and_content(self):
        package = dataclasses.replace(p("26.04.10.1", release="0", name="ICAClient"), sha256="a" * 64)
        expected = Path("Packages/ICAClient-26.04.10.1-0.x86_64-aaaaaaaaaaaaaaaa.rpm")
        self.assertEqual(package.public_path, expected)
        for filename in ("upstream.rpm", "a" * 64 + "-upstream.rpm", expected.name):
            self.assertEqual(dataclasses.replace(package, path=Path(filename)).public_path, expected)
        self.assertNotEqual(dataclasses.replace(package, sha256="b" * 64).public_path, expected)
        self.assertIn("-2_26.04.10.1-", str(dataclasses.replace(package, evr=("2", "26.04.10.1", "0")).public_path))

    def test_reject_unsafe_filenames(self):
        for filename in ("../bad.rpm", "/tmp/bad.rpm", "..%2Fbad.rpm", "bad\n.rpm", "file.exe"):
            with self.assertRaises(mirror.SyncError):
                mirror.safe_filename(filename)

    def test_signed_url_tokens_do_not_change_asset_identity(self):
        a = mirror.Asset("file.rpm", "https://example.test/file.rpm?token=a", {"x"}, "a" * 64)
        b = dataclasses.replace(a, url="https://example.test/file.rpm?token=b")
        self.assertEqual(a.source_id, b.source_id)
        self.assertNotIn("token", mirror.clean_url(a.url))


class DiscoveryTests(unittest.TestCase):
    def test_bitwarden_module_resolves_official_filename_and_digest(self):
        response = mock.MagicMock()
        response.headers = {"Content-Disposition": 'attachment; filename="Bitwarden-2026.9.1-x86_64.rpm"', "Content-Length": "123"}
        response.url = "https://example.test/file.rpm"
        response.__enter__.return_value = response
        session = mock.Mock()
        session.get.return_value = response
        with mock.patch.object(BITWARDEN.module, "json_get", return_value={"assets": [{"name": "Bitwarden-2026.9.1-x86_64.rpm", "digest": "sha256:" + "a" * 64, "size": 123}]}):
            asset = BITWARDEN.discover(session)[0]
        self.assertEqual(asset.filename, "Bitwarden-2026.9.1-x86_64.rpm")
        self.assertEqual(asset.checksum, "a" * 64)
        self.assertEqual(asset.names, {"bitwarden"})

    def test_citrix_selects_rpm_sections_and_checksum(self):
        parts = []
        for heading, name in (("RedHat Full Package", "ICAClient"), ("USB Support Package", "ctxusb"),
                              ("App Protection Package", "ctxappprotection")):
            parts.append(f'<div class="ctx-dl-details"><h4>{heading} (x86_64)</h4>'
                         f'<a class="ctx-dl-link" rel="//downloads.citrix.com/{name}.deb">DEB</a></div>')
            parts.append(f'<div class="ctx-dl-details"><h4>{heading} (x86_64)</h4>'
                         f'<a class="ctx-dl-link" rel="//downloads.citrix.com/{name}.rpm?token=secret">RPM</a>'
                         f'SHA-256 - {"a" * 64}</div>')
        response = mock.MagicMock(text="".join(parts))
        response.__enter__.return_value = response
        s = mock.Mock()
        s.get.return_value = response
        assets = CITRIX.discover(s)
        self.assertEqual(len(assets), 3)
        self.assertTrue(all(a.checksum == "a" * 64 for a in assets))
        self.assertTrue(all(a.url.startswith("https://downloads.citrix.com/") for a in assets))

    def test_rambox_rejects_missing_x64_asset(self):
        with mock.patch.object(RAMBOX.module, "json_get", return_value={"assets": [{"name": "Rambox-arm64.rpm"}]}):
            with self.assertRaises(mirror.SyncError):
                RAMBOX.discover(None)

    def test_rambox_falls_back_to_latest_stable_rpm(self):
        asset = {"name": "Rambox-2.7.1-linux-x64.rpm", "browser_download_url": "https://example.test/app.rpm"}
        latest = {"tag_name": "v3.0.0", "assets": []}
        beta = {"tag_name": "v2.7.2-beta.1", "prerelease": True, "assets": [asset]}
        stable = {"tag_name": "v2.7.1", "assets": [asset]}
        with mock.patch.object(RAMBOX.module, "json_get", side_effect=[latest, [latest, beta, stable]]):
            found = RAMBOX.discover(None)
        self.assertEqual(found[0].filename, "Rambox-2.7.1-linux-x64.rpm")
        self.assertEqual(found[0].revision, "v2.7.1")

    def test_rambox_accepts_generic_single_rpm_filename(self):
        release = {"tag_name": "v4", "assets": [{"name": "Rambox-4.rpm", "browser_download_url": "https://example.test/app.rpm"}]}
        with mock.patch.object(RAMBOX.module, "json_get", return_value=release):
            self.assertEqual(RAMBOX.discover(None)[0].filename, "Rambox-4.rpm")


@unittest.skipUnless(shutil.which("rpmbuild") and shutil.which("createrepo_c"), "rpm-build and createrepo_c required")
class IntegrationTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.fixture_dir = tempfile.TemporaryDirectory(prefix="rpm-mirror-fixtures-")
        root = Path(cls.fixture_dir.name)
        cls.fixtures = {}
        for version in ("1.0", "2.0", "3.0", "4.0", "5.0"):
            build = root / version
            build.mkdir()
            specfile = build / "fixture.spec"
            specfile.write_text(f'''Name: bitwarden
Version: {version}
Release: 1
Summary: RPM mirror integration fixture
License: MIT
BuildArch: noarch
%description
Small real RPM for repository update tests.
%install
mkdir -p %{{buildroot}}/usr/share/mirror-fixture
echo {version} > %{{buildroot}}/usr/share/mirror-fixture/version.txt
%files
/usr/share/mirror-fixture/version.txt
''')
            command = ["rpmbuild", "-bb", "--define", "_topdir " + str(build),
                       "--define", "__os_install_post %{nil}", "--define", "__spec_install_post %{nil}",
                       "--define", "_build_id_links none", str(specfile)]
            built = subprocess.run(command, capture_output=True, text=True)
            if built.returncode:
                raise RuntimeError(built.stdout + built.stderr)
            cls.fixtures[version] = next((build / "RPMS/noarch").glob("*.rpm"))

    @classmethod
    def tearDownClass(cls):
        cls.fixture_dir.cleanup()

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="rpm-mirror-test-")
        self.root = Path(self.temp.name)
        self.web = self.root / "www/html"
        self.repo = self.web / "bitwarden"
        self.repo.mkdir(parents=True)
        self.state = self.root / "state"

    def tearDown(self):
        self.temp.cleanup()

    @contextlib.contextmanager
    def serve(self, data, truncated=False):
        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):
                self.send_response(200)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data[:len(data) // 2] if truncated else data)
                self.close_connection = True

            def log_message(self, *args):
                pass
        server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            yield "http://127.0.0.1:%d/file.rpm" % server.server_port
        finally:
            server.shutdown()
            server.server_close()
            thread.join()

    def local_session(self):
        s = requests.Session()
        s.trust_env = False
        return s

    def seed(self, versions):
        for version in versions:
            dest = self.repo / ("Bitwarden-" + version + "-x86_64.rpm")
            shutil.copyfile(self.fixtures[version], dest)

    def asset(self, version, url, digest=True):
        fixture = self.fixtures[version]
        return mirror.Asset("Bitwarden-" + version + "-x86_64.rpm", url, {"bitwarden"},
                            mirror.sha256(fixture) if digest else None, fixture.stat().st_size, "revision1")

    def run_sync(self, asset, dry_run=False):
        with mock.patch.object(mirror, "session", side_effect=self.local_session), \
             mock.patch.object(BITWARDEN.module, "discover", return_value=[asset]):
            mirror.sync(BITWARDEN, self.web, self.state, keep=3, dry_run=dry_run)

    def test_real_rpm_validation_rejects_corrupt_payload(self):
        path = self.root / "corrupt.rpm"
        data = bytearray(self.fixtures["4.0"].read_bytes())
        data[-16] ^= 0xFF
        path.write_bytes(data)
        with self.assertRaises(mirror.SyncError):
            mirror.inspect_package(path, {"bitwarden"})

    def test_engine_supports_an_additional_service_without_core_changes(self):
        from common.registry import Service
        config = dataclasses.replace(BITWARDEN.config, id="demo")
        with self.serve(self.fixtures["4.0"].read_bytes()) as url:
            module = mock.Mock()
            module.discover.return_value = [self.asset("4.0", url)]
            with mock.patch.object(mirror, "session", side_effect=self.local_session):
                mirror.sync(Service(config, module), self.web, self.state)
        packages = mirror.inventory(self.web / "demo", config.package_names)
        self.assertEqual(len(packages), 1)
        mirror.validate_metadata(self.web / "demo", packages)
        self.assertTrue((self.state / "demo/state.json").is_file())

    def test_migration_three_versions_original_bytes_and_noop(self):
        self.seed(("1.0", "2.0", "3.0"))
        expected = self.fixtures["4.0"].read_bytes()
        with self.serve(expected) as url:
            asset = self.asset("4.0", url)
            self.run_sync(asset)
            packages = mirror.inventory(self.repo, BITWARDEN.config.package_names)
            self.assertEqual({p.evr[1] for p in packages}, {"2.0", "3.0", "4.0"})
            self.assertEqual(len(list(self.repo.glob("*.rpm"))), 0)
            fresh = next(p for p in packages if p.evr[1] == "4.0")
            self.assertEqual(fresh.path.read_bytes(), expected)
            mirror.validate_metadata(self.repo, packages)
            before = (self.repo / "repodata/repomd.xml").stat().st_mtime_ns
            with mock.patch.object(mirror, "download", wraps=mirror.download) as dl, \
                 mock.patch.object(mirror.subprocess, "run", wraps=subprocess.run) as command:
                self.run_sync(asset)
                self.assertEqual(dl.call_count, 1)
                self.assertFalse(any(call.args[0][0] == "createrepo_c" for call in command.call_args_list))
            self.assertEqual((self.repo / "repodata/repomd.xml").stat().st_mtime_ns, before)

    def test_hash_prefixed_repository_migrates_to_readable_paths(self):
        self.seed(("1.0", "2.0", "3.0"))
        with self.serve(self.fixtures["3.0"].read_bytes()) as url:
            asset = self.asset("3.0", url)
            self.run_sync(asset)
            packages = mirror.inventory(self.repo, BITWARDEN.config.package_names)
            before = {package.evr: package.path.read_bytes() for package in packages}
            legacy_paths = []
            for package in packages:
                legacy = package.path.parent / (package.sha256 + "-Bitwarden-" + package.evr[1] + "-x86_64.rpm")
                package.path.rename(legacy)
                legacy_paths.append(legacy)
            subprocess.run(["createrepo_c", "--no-database", str(self.repo)], check=True, capture_output=True)
            with mock.patch.object(mirror, "inspect_package", wraps=mirror.inspect_package) as inspected:
                self.run_sync(asset)
                self.assertFalse(any(call.kwargs.get("fresh") is True for call in inspected.call_args_list))
            packages = mirror.inventory(self.repo, BITWARDEN.config.package_names)
            self.assertEqual({package.evr: package.path.read_bytes() for package in packages}, before)
            self.assertTrue(all(package.path.name.startswith("bitwarden-") for package in packages))
            self.assertTrue(all(not path.exists() for path in legacy_paths))
            mirror.validate_metadata(self.repo, packages)
            self.assertEqual(len(packages), 3)

    def test_existing_package_url_with_changed_bytes_is_not_overwritten(self):
        self.seed(("1.0", "2.0", "3.0"))
        with self.serve(self.fixtures["4.0"].read_bytes()) as url:
            asset = self.asset("4.0", url)
            self.run_sync(asset)
            before = (self.repo / "repodata/repomd.xml").read_bytes()
            package = next(p for p in mirror.inventory(self.repo, BITWARDEN.config.package_names) if p.evr[1] == "4.0")
            changed = self.fixtures["1.0"].read_bytes()
            package.path.write_bytes(changed)
            with self.assertRaises(mirror.SyncError):
                self.run_sync(asset)
            self.assertEqual(package.path.read_bytes(), changed)
            self.assertEqual((self.repo / "repodata/repomd.xml").read_bytes(), before)

    def test_truncated_download_does_not_modify_public_repo(self):
        self.seed(("1.0", "2.0", "3.0"))
        before = {p.name: p.read_bytes() for p in self.repo.glob("*.rpm")}
        with self.serve(self.fixtures["4.0"].read_bytes(), truncated=True) as url, \
             mock.patch.object(mirror.time, "sleep"):
            with self.assertRaises(requests.RequestException):
                self.run_sync(self.asset("4.0", url))
        self.assertEqual({p.name: p.read_bytes() for p in self.repo.glob("*.rpm")}, before)
        self.assertFalse((self.repo / "repodata").exists())

    def test_checksum_failure_does_not_publish_or_prune(self):
        self.seed(("1.0", "2.0", "3.0"))
        with self.serve(self.fixtures["4.0"].read_bytes()) as url, mock.patch.object(mirror.time, "sleep"):
            asset = dataclasses.replace(self.asset("4.0", url), checksum="f" * 64)
            with self.assertRaises(mirror.SyncError):
                self.run_sync(asset)
        self.assertEqual(len(list(self.repo.glob("*.rpm"))), 3)
        self.assertFalse((self.repo / "repodata").exists())

    def test_failed_metadata_build_preserves_published_repo(self):
        self.seed(("1.0", "2.0", "3.0"))
        self.run_sync(self.asset("3.0", "http://unused.test", digest=True))
        old_repomd = (self.repo / "repodata/repomd.xml").read_bytes()
        old_packages = {str(p.relative_to(self.repo)): p.read_bytes() for p in (self.repo / "Packages").glob("*.rpm")}
        real_run = subprocess.run
        def fail_build(command, **kwargs):
            if command[0] == "createrepo_c":
                raise subprocess.CalledProcessError(1, command)
            return real_run(command, **kwargs)
        with self.serve(self.fixtures["4.0"].read_bytes()) as url, \
             mock.patch.object(mirror.subprocess, "run", side_effect=fail_build):
            with self.assertRaises(subprocess.CalledProcessError):
                self.run_sync(self.asset("4.0", url))
        self.assertEqual((self.repo / "repodata/repomd.xml").read_bytes(), old_repomd)
        self.assertEqual({str(p.relative_to(self.repo)): p.read_bytes() for p in (self.repo / "Packages").glob("*.rpm")}, old_packages)

    def test_dry_run_makes_no_public_changes(self):
        self.seed(("1.0", "2.0", "3.0"))
        with self.serve(self.fixtures["4.0"].read_bytes()) as url:
            self.run_sync(self.asset("4.0", url), dry_run=True)
        self.assertEqual(len(list(self.repo.glob("*.rpm"))), 3)
        self.assertFalse((self.repo / "Packages").exists())
        self.assertFalse((self.state / "bitwarden/state.json").exists())

    def test_lock_rejects_overlapping_runs(self):
        state = self.state / "bitwarden"
        state.mkdir(parents=True)
        with mirror.repo_lock(state):
            with self.assertRaises(mirror.SyncError):
                with mirror.repo_lock(state):
                    self.fail("Acquired duplicate lock")

    def test_second_update_retains_three_and_previous_metadata(self):
        self.seed(("1.0", "2.0", "3.0"))
        self.run_sync(self.asset("3.0", "http://unused.test"))
        old_refs = mirror.metadata_refs(self.repo / "repodata/repomd.xml")
        with self.serve(self.fixtures["4.0"].read_bytes()) as url:
            self.run_sync(self.asset("4.0", url))
        for href in old_refs:
            self.assertTrue((self.repo / href).exists())
        self.assertEqual({p.evr[1] for p in mirror.inventory(self.repo, BITWARDEN.config.package_names)}, {"2.0", "3.0", "4.0"})

    def test_no_digest_source_revision_is_saved_on_unchanged_run(self):
        self.seed(("1.0", "2.0", "3.0"))
        with self.serve(self.fixtures["4.0"].read_bytes()) as url:
            asset = self.asset("4.0", url, digest=False)
            self.run_sync(asset)
            asset.revision = "revision2"
            self.run_sync(asset)
            state = json.loads((self.state / "bitwarden/state.json").read_text())
            self.assertIn(asset.source_id, state["sources"])

    def test_killed_process_staging_is_cleaned(self):
        self.seed(("1.0", "2.0", "3.0"))
        stale = self.web.parent / ".rpm-repo-sync-work/bitwarden-stale"
        stale.mkdir(parents=True)
        (stale / "partial-download").write_bytes(b"old partial data")
        self.run_sync(self.asset("3.0", "http://unused.test"))
        self.assertFalse(stale.exists())

    def test_invalid_generated_metadata_cannot_publish(self):
        self.seed(("1.0", "2.0", "3.0"))
        with mock.patch.object(mirror, "validate_metadata", side_effect=mirror.SyncError("bad metadata")):
            with self.assertRaises(mirror.SyncError):
                self.run_sync(self.asset("3.0", "http://unused.test"))
        self.assertEqual(len(list(self.repo.glob("*.rpm"))), 3)
        self.assertFalse((self.repo / "repodata").exists())


if __name__ == "__main__":
    unittest.main()
