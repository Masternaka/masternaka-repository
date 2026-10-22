import hashlib
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import Mock, patch
import urllib.request

from tools.collect_packages import CollectionError, SafeRedirect, collect, load_config, request_bytes, validate_url


class CollectorTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.config = self.root / 'apps.json'
        self.app = {'repository': 'Masternaka/MintTweaks', 'package': 'minttweaks', 'architecture': 'all'}
        self.config.write_text(json.dumps({'apps': [self.app]}))
        self.output = self.root / 'packages'
        self.data = b'fake-deb-for-offline-tests'
        self.asset = {'name': 'minttweaks_0.1.2+git3.1.abcdef_all.deb', 'digest': 'sha256:' + hashlib.sha256(self.data).hexdigest(), 'browser_download_url': 'https://github.com/Masternaka/MintTweaks/releases/download/v0.1.2-build.3.1/minttweaks_0.1.2+git3.1.abcdef_all.deb'}
        self.release = {'draft': False, 'prerelease': False, 'tag_name': 'v0.1.2-build.3.1', 'assets': [self.asset]}
        self.identity = Mock(return_value={'Package': 'minttweaks', 'Architecture': 'all', 'Version': '0.1.2+git3.1.abcdef'})
        self.fetch = Mock(side_effect=lambda url, **kwargs: json.dumps(self.release).encode() if kwargs.get('api') else self.data)

    def run_collection(self):
        return collect(self.config, self.output, token='private-api-token', fetch=self.fetch, identity=self.identity)

    def test_complete_snapshot_and_provenance_without_credentials(self):
        manifest = self.run_collection()
        self.assertEqual((self.output / self.asset['name']).read_bytes(), self.data)
        self.assertEqual(json.loads((self.output / 'manifest.json').read_text())['apps'], manifest)
        self.assertEqual(manifest[0]['repository'], self.app['repository'])
        self.assertNotIn('private-api-token', (self.output / 'manifest.json').read_text())
        self.assertNotIn('token', self.fetch.call_args_list[1].kwargs)

    def test_duplicate_apps_and_bad_configuration_rejected(self):
        for config in ({'apps': []}, {'apps': [self.app, self.app]}, {'apps': [{**self.app, 'architecture': 'amd64'}]}, {'apps': [self.app], 'extra': True}, {'apps': [{**self.app, 'package': '../evil'}]}):
            with self.subTest(config=config):
                self.config.write_text(json.dumps(config))
                with self.assertRaises(CollectionError):
                    load_config(self.config)
        self.config.write_text('{"apps": [], "apps": []}')
        with self.assertRaises(CollectionError):
            load_config(self.config)

    def test_missing_digest_and_mismatch_remove_output(self):
        for digest in (None, 'sha256:' + '0' * 64):
            self.asset['digest'] = digest
            with self.assertRaises(CollectionError):
                self.run_collection()
            self.assertFalse(self.output.exists())

    def test_identity_and_version_mismatch_remove_output(self):
        for field, value in (('Package', 'other'), ('Architecture', 'amd64'), ('Version', '9.9')):
            with self.subTest(field=field):
                self.identity.return_value = {'Package': 'minttweaks', 'Architecture': 'all', 'Version': '0.1.2+git3.1.abcdef', field: value}
                with self.assertRaises(CollectionError):
                    self.run_collection()
                self.assertFalse(self.output.exists())

    def test_missing_or_ambiguous_asset_and_prerelease_rejected(self):
        for assets in ([], [self.asset, self.asset]):
            self.release['assets'] = assets
            with self.assertRaises(CollectionError):
                self.run_collection()
            self.assertFalse(self.output.exists())
        self.release['assets'] = [self.asset]
        self.release['prerelease'] = True
        with self.assertRaises(CollectionError):
            self.run_collection()

    def test_api_failure_never_produces_partial_snapshot(self):
        self.config.write_text(json.dumps({'apps': [self.app, {'repository': 'Masternaka/Other', 'package': 'other', 'architecture': 'all'}]}))
        original_fetch = self.fetch.side_effect
        def fetch(url, **kwargs):
            if '/Other/' in url:
                raise CollectionError('API 404')
            return original_fetch(url, **kwargs)
        self.fetch.side_effect = fetch
        with self.assertRaisesRegex(CollectionError, 'API 404'):
            self.run_collection()
        self.assertFalse(self.output.exists())

    def test_untrusted_urls_and_cross_repo_assets_rejected(self):
        for url in ('http://github.com/a', 'https://evil.example/file', 'https://github.com@evil.example/file', 'https://github.com:444/a'):
            with self.assertRaises(CollectionError):
                validate_url(url)
        self.asset['browser_download_url'] = 'https://github.com/Other/Repo/releases/download/tag/a.deb'
        with self.assertRaises(CollectionError):
            self.run_collection()

    def test_token_only_attached_to_api_requests(self):
        response = Mock()
        response.__enter__ = Mock(return_value=response)
        response.__exit__ = Mock(return_value=False)
        response.read.return_value = b'{}'
        opener = Mock()
        opener.open.return_value = response
        with patch('urllib.request.build_opener', return_value=opener):
            request_bytes('https://api.github.com/repos/Masternaka/MintTweaks/releases/latest', token='secret', api=True)
            self.assertEqual(opener.open.call_args.args[0].get_header('Authorization'), 'Bearer secret')
            request_bytes(self.asset['browser_download_url'], token='secret', api=False)
            self.assertIsNone(opener.open.call_args.args[0].get_header('Authorization'))
        request = urllib.request.Request('https://api.github.com/test', headers={'Authorization': 'Bearer secret'})
        with self.assertRaises(CollectionError):
            SafeRedirect().redirect_request(request, None, 302, '', {}, 'https://release-assets.githubusercontent.com/file')


if __name__ == '__main__':
    unittest.main()
