"""Exact upstream selection is opt-in, provider-specific and never persisted."""
import contextlib
import io
from pathlib import Path
import tempfile
import unittest
from unittest import mock

import requests
from common import cli, engine
from common.registry import load_service

ROOT = Path(__file__).resolve().parents[1]
API = 'https://api.github.com/repos/ramboxapp/download/releases'


def release(tag='v3.1.1'):
    return {'tag_name': tag, 'assets': [{
        'name': 'Rambox-' + tag.lstrip('v') + '-linux-x64.rpm',
        'browser_download_url': 'https://example.test/package.rpm',
        'size': 123, 'digest': 'sha256:' + 'a' * 64,
    }]}


class ReleaseTests(unittest.TestCase):
    def setUp(self):
        self.service = load_service(ROOT / 'services', 'rambox')
        self.service.release_tag = 'v3.1.1'

    def test_exact_tag_bypasses_latest_and_preserves_digest(self):
        with mock.patch.object(self.service.module, 'json_get', return_value=release()) as get:
            asset = self.service.discover(None)[0]
        get.assert_called_once_with(None, API + '/tags/v3.1.1')
        self.assertEqual(asset.filename, 'Rambox-3.1.1-linux-x64.rpm')
        self.assertEqual(asset.checksum, 'a' * 64)
        self.assertEqual(asset.revision, 'v3.1.1')

    def test_exact_tag_without_rpm_fails_without_fallback(self):
        with mock.patch.object(self.service.module, 'json_get', return_value={'tag_name': 'v3.1.1', 'assets': []}) as get:
            with self.assertRaisesRegex(engine.SyncError, 'no compatible RPM'):
                self.service.discover(None)
        self.assertEqual(get.call_count, 1)

    def test_missing_tag_http_error_is_not_replaced_with_latest(self):
        with mock.patch.object(self.service.module, 'json_get', side_effect=requests.HTTPError('404')) as get:
            with self.assertRaises(requests.HTTPError):
                self.service.discover(None)
        self.assertEqual(get.call_count, 1)

    def test_requested_draft_or_prerelease_is_rejected(self):
        for flag in ('draft', 'prerelease'):
            data = release()
            data[flag] = True
            with self.subTest(flag=flag), mock.patch.object(self.service.module, 'json_get', return_value=data):
                with self.assertRaisesRegex(engine.SyncError, 'draft or prerelease'):
                    self.service.discover(None)

    def test_tag_is_encoded_as_one_api_path_component(self):
        self.service.release_tag = 'series/3.1.1'
        data = release()
        data['tag_name'] = self.service.release_tag
        with mock.patch.object(self.service.module, 'json_get', return_value=data) as get:
            self.service.discover(None)
        get.assert_called_once_with(None, API + '/tags/series%2F3.1.1')

    def test_different_returned_tag_is_rejected(self):
        with mock.patch.object(self.service.module, 'json_get', return_value=release('v2.7.2')):
            with self.assertRaisesRegex(engine.SyncError, 'did not return the requested'):
                self.service.discover(None)

    def test_cli_release_requires_one_service_and_nonempty_tag(self):
        for args in (['--release', 'v3.1.1'], ['--repo', 'rambox', '--release', ''],
                     ['--repo', 'rambox', '--release', 'v3.1.1', '--list']):
            with self.subTest(args=args), contextlib.redirect_stderr(io.StringIO()):
                with self.assertRaises(SystemExit) as done:
                    cli.main(args)
                self.assertEqual(done.exception.code, 2)

    def test_unsupported_provider_does_not_start_sync(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            with mock.patch.object(engine, 'sync') as sync, contextlib.redirect_stdout(io.StringIO()):
                result = cli.main(['--repo', 'bitwarden', '--release', 'v1',
                                   '--state-dir', str(root / 'state'), '--log-dir', str(root / 'logs')])
            self.assertEqual(result, 1)
            sync.assert_not_called()
            self.assertIn('not supported', (root / 'logs/bitwarden.log').read_text())

    def test_next_run_without_flag_uses_latest_again(self):
        observed = []
        def sync(service, *args):
            observed.append(service.discover(None)[0].filename)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            args = ['--repo', 'rambox', '--state-dir', str(root / 'state'), '--log-dir', str(root / 'logs')]
            manifest = ROOT / 'services/rambox/service.json'
            before = manifest.read_bytes()
            with mock.patch('common.http.json_get', side_effect=[release(), release('v2.7.2')]) as get, \
                 mock.patch.object(engine, 'sync', side_effect=sync), contextlib.redirect_stdout(io.StringIO()):
                self.assertEqual(cli.main([*args, '--release', 'v3.1.1']), 0)
                self.assertEqual(cli.main(args), 0)
            self.assertEqual(get.call_args_list, [mock.call(None, API + '/tags/v3.1.1'), mock.call(None, API + '/latest')])
            self.assertEqual(observed, ['Rambox-3.1.1-linux-x64.rpm', 'Rambox-2.7.2-linux-x64.rpm'])
            self.assertEqual(manifest.read_bytes(), before)
