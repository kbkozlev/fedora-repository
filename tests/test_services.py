"""Separate services, selective deployment and cron migration."""
import contextlib
import io
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from common import cli, engine
from common.config import read_config, service_ids
from common.install_config import deploy_code, migrate_cron, plan_install
from common.registry import load_service


class ServiceTests(unittest.TestCase):
    def test_selective_install_has_only_selected_dependencies(self):
        with tempfile.TemporaryDirectory() as tmp:
            plan = plan_install(ROOT / "services", Path(tmp), ["bitwarden"])
        self.assertEqual(plan["selected"], ["bitwarden"])
        self.assertEqual(plan["deploy"], ["bitwarden"])
        self.assertNotIn("python3-beautifulsoup4", plan["dependencies"])
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIn("python3-beautifulsoup4", plan_install(ROOT / "services", Path(tmp), ["citrix"])["dependencies"])

    def test_unknown_service_is_rejected_before_install(self):
        with tempfile.TemporaryDirectory() as tmp:
            with self.assertRaises(ValueError):
                plan_install(ROOT / "services", Path(tmp), ["../bad"])

    def test_selective_deployment_installs_only_selected_module_and_runs(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            deploy_code(ROOT, target, ["bitwarden"])
            self.assertEqual(service_ids(target / "services"), ["bitwarden"])
            self.assertTrue((target / "common/engine.py").is_file())
            self.assertEqual((target / "services/bitwarden/update.sh").stat().st_mode & 0o777, 0o755)
            result = subprocess.run([sys.executable, str(target / "rpm_repo_sync.py"), "--list"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("bitwarden", result.stdout)
            self.assertNotIn("citrix", result.stdout)
            self.assertNotIn("rambox", result.stdout)

    def test_deployment_preserves_unselected_module_and_repo_state(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            deploy_code(ROOT, target, ["citrix"])
            module = target / "services/citrix/service.py"
            module.write_text("# independently modified Citrix downloader\n")
            state = target / "state.json"
            state.write_text('{"accepted": "unchanged"}')
            deploy_code(ROOT, target, ["bitwarden"])
            self.assertEqual(module.read_text(), "# independently modified Citrix downloader\n")
            self.assertEqual(state.read_text(), '{"accepted": "unchanged"}')

    def test_updating_one_service_does_not_redeploy_other_installed_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            shutil.copytree(ROOT / "services/citrix", target / "services/citrix")
            before = (target / "services/citrix/service.py").read_bytes()
            plan = plan_install(ROOT / "services", target, ["bitwarden"])
            self.assertEqual(plan["deploy"], ["bitwarden"])
            self.assertEqual((target / "services/citrix/service.py").read_bytes(), before)

    def test_migrate_bundled_install_preserves_previously_installed_services(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            (target / "rpm_repo_sync.py").write_text('APPS = ("bitwarden", "citrix", "rambox")\n')
            plan = plan_install(ROOT / "services", target, ["bitwarden"])
        self.assertEqual(plan["selected"], ["bitwarden"])
        self.assertEqual(plan["deploy"], ["bitwarden", "citrix", "rambox"])
        self.assertEqual(plan["migrated"], ["bitwarden", "citrix", "rambox"])
        self.assertEqual(plan["schedules"], {"bitwarden": "00:00"})

    def test_cron_replaces_only_selected_schedule_without_duplicates(self):
        previous = ('0 0 * * * /bin/bash /root/bitwarden/update-bitwarden-repo.sh\n'
                    '10 0 * * * /bin/bash /root/citrix/update-citrix-repo.sh\n'
                    '5 1 * * * /root/backup.sh\n'
                    '# /root/bitwarden/update-bitwarden-repo.sh is a managed task\n')
        changed = migrate_cron(previous, {"bitwarden": "02:35"})
        self.assertIn('35 2 * * * /bin/bash /root/bitwarden/update-bitwarden-repo.sh', changed)
        self.assertIn('10 0 * * * /bin/bash /root/citrix/update-citrix-repo.sh', changed)
        self.assertIn('5 1 * * * /root/backup.sh', changed)
        self.assertIn('# /root/bitwarden/', changed)
        self.assertEqual(migrate_cron(changed, {"bitwarden": "02:35"}), changed)

    def test_new_service_is_discovered_without_core_registry_changes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = root / "demo"
            app.mkdir()
            (app / "service.json").write_text(json.dumps({"id": "demo", "package_names": ["DemoApp"], "update_time": "01:30"}))
            (app / "service.py").write_text('from common.models import Asset\ndef discover(session, config):\n    return [Asset("demo.rpm", "https://example.test/demo.rpm", config.package_names)]\n')
            service = load_service(root, "demo")
            self.assertEqual(service_ids(root), ["demo"])
            self.assertEqual(service.discover(None)[0].names, {"demoapp"})
            self.assertEqual(service.config.update_time, "01:30")

    def test_config_rejects_unsafe_names_and_invalid_schedules(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / "demo"
            app.mkdir()
            (app / "service.py").write_text('def discover(session, config): return []\n')
            for updates in ({"id": "../demo"}, {"package_names": ["../bad"]}, {"update_time": "24:00"}, {"dependencies": ["pkg;echo bad"]}):
                config = {"id": "demo", "package_names": ["demo"], "update_time": "00:00"}
                config.update(updates)
                (app / "service.json").write_text(json.dumps(config))
                with self.assertRaises(ValueError):
                    read_config(app)

    def test_template_requires_a_configured_vendor_url(self):
        service = load_service(ROOT / "examples", "myapp")
        with self.assertRaises(engine.SyncError):
            service.discover(mock.Mock())

    def test_fixed_latest_template_uses_revision_or_forces_revalidation(self):
        service = load_service(ROOT / "examples", "myapp")
        response = mock.MagicMock()
        response.headers = {"Content-Length": "123"}
        response.url = "https://vendor.test/myapp-latest.rpm"
        response.__enter__.return_value = response
        session = mock.Mock()
        session.get.return_value = response
        with mock.patch.object(service.module, "RPM_URL", response.url), mock.patch.object(service.module.time, "time_ns", side_effect=[100, 200]):
            first, second = service.discover(session)[0], service.discover(session)[0]
        self.assertNotEqual(first.source_id, second.source_id)
        response.headers["ETag"] = '"version1"'
        with mock.patch.object(service.module, "RPM_URL", response.url):
            first, second = service.discover(session)[0], service.discover(session)[0]
        self.assertEqual(first.source_id, second.source_id)

    def test_single_service_run_does_not_import_broken_unrelated_service(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / "services/bitwarden", root / "services/bitwarden")
            shutil.copytree(ROOT / "services/citrix", root / "services/citrix")
            (root / "services/citrix/service.py").write_text('raise ImportError("unavailable unrelated dependency")\n')
            with mock.patch.object(engine, "sync") as sync, contextlib.redirect_stdout(io.StringIO()):
                result = cli.main(["--repo", "bitwarden", "--services-dir", str(root / "services"), "--state-dir", str(root / "state"), "--log-dir", str(root / "logs")])
            self.assertEqual(result, 0)
            self.assertEqual(sync.call_args.args[0].config.id, "bitwarden")
            self.assertFalse((root / "logs/citrix.log").exists())

    def test_all_runs_only_services_present_in_installed_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            shutil.copytree(ROOT / "services/rambox", root / "services/rambox")
            with mock.patch.object(engine, "sync") as sync, contextlib.redirect_stdout(io.StringIO()):
                result = cli.main(["--repo", "all", "--services-dir", str(root / "services"), "--state-dir", str(root / "state"), "--log-dir", str(root / "logs")])
            self.assertEqual(result, 0)
            self.assertEqual(sync.call_count, 1)
            self.assertEqual(sync.call_args.args[0].config.id, "rambox")

    def test_failed_service_does_not_prevent_other_services_running(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            for name in ("bitwarden", "citrix", "rambox"):
                shutil.copytree(ROOT / "services" / name, root / "services" / name)
            seen = []
            def sync(service, *args):
                seen.append(service.config.id)
                if service.config.id == "bitwarden": raise RuntimeError("failed download")
            with mock.patch.object(engine, "sync", side_effect=sync), contextlib.redirect_stdout(io.StringIO()):
                result = cli.main(["--repo", "all", "--services-dir", str(root / "services"), "--state-dir", str(root / "state"), "--log-dir", str(root / "logs")])
            self.assertEqual(result, 1)
            self.assertEqual(seen, ["bitwarden", "citrix", "rambox"])

    def test_installation_requires_explicit_selection(self):
        result = subprocess.run(["bash", str(ROOT / "install.sh")], capture_output=True, text=True)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Usage:", result.stderr)

    def test_each_service_has_an_independent_install_entry_point(self):
        for name in service_ids(ROOT / "services"):
            result = subprocess.run(["bash", str(ROOT / "services" / name / "install.sh"), "--help"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0)
            self.assertIn("Usage:", result.stdout)
