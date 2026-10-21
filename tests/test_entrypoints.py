"""Direct per-service Python entry points, independent of working directory."""
import contextlib
import io
import json
from pathlib import Path
import runpy
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

from common import engine
from common.config import read_config
from common.install_config import deploy_code

ROOT = Path(__file__).resolve().parents[1]


class EntrypointTests(unittest.TestCase):
    def test_each_python_entrypoint_updates_only_its_own_repository(self):
        for name in ('bitwarden', 'citrix', 'rambox'):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                args = ['update.py', '--state-dir', str(root / 'state'), '--log-dir', str(root / 'logs'), '--dry-run']
                with mock.patch.object(sys, 'argv', args), mock.patch.object(engine, 'sync') as sync, contextlib.redirect_stdout(io.StringIO()):
                    with self.assertRaises(SystemExit) as done:
                        runpy.run_path(str(ROOT / 'services' / name / 'update.py'), run_name='__main__')
                self.assertEqual(done.exception.code, 0)
                self.assertEqual(sync.call_count, 1)
                self.assertEqual(sync.call_args.args[0].config.id, name)
                self.assertEqual(sync.call_args.args[3], 3)
                self.assertTrue(sync.call_args.args[4])

    def test_installed_python_symlink_runs_from_unrelated_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            deploy_code(ROOT, root / 'installed', ['bitwarden'])
            link = root / 'update-bitwarden-repo.py'
            link.symlink_to(root / 'installed/services/bitwarden/update.py')
            result = subprocess.run([sys.executable, str(link), '--help'], cwd='/tmp', capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn('--dry-run', result.stdout)

    def test_updating_one_module_preserves_other_python_entrypoints(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp)
            deploy_code(ROOT, target, ['citrix'])
            other = target / 'services/citrix/update.py'
            other.write_text('# operator custom entrypoint\n')
            deploy_code(ROOT, target, ['bitwarden'])
            self.assertEqual(other.read_text(), '# operator custom entrypoint\n')

    def test_legacy_schedule_field_has_no_runtime_effect(self):
        with tempfile.TemporaryDirectory() as tmp:
            app = Path(tmp) / 'demo'
            app.mkdir()
            (app / 'service.py').write_text('def discover(session, config): return []\n')
            (app / 'service.json').write_text(json.dumps({'id': 'demo', 'package_names': ['demo'], 'update_time': '00:00'}))
            config = read_config(app)
            self.assertEqual(config.id, 'demo')
            self.assertFalse(hasattr(config, 'update_time'))
