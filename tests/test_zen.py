import hashlib
import io
import json
from pathlib import Path
import struct
import platform
import shutil
import sys
import time
import tarfile
import tempfile
import unittest
from unittest.mock import patch, Mock

from tools.collect_packages import CollectionError, collect, load_config
from tools.package_zen import PackagingError, build_package, debian_version, dependencies, elf_files, merge_policies, safe_extract


def fake_elf(machine=62):
    header = bytearray(64)
    header[:6] = b'\x7fELF\x02\x01'
    struct.pack_into('<H', header, 18, machine)
    return bytes(header)


class ZenTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.archive = self.root / 'zen.tar.xz'

    def archive_with(self, entries):
        with tarfile.open(self.archive, 'w:xz') as archive:
            for name, kind, content in entries:
                member = tarfile.TarInfo(name)
                member.type = kind
                member.mode = 0o6777
                if kind == tarfile.REGTYPE:
                    member.size = len(content)
                    archive.addfile(member, io.BytesIO(content))
                elif kind in (tarfile.SYMTYPE, tarfile.LNKTYPE):
                    member.linkname = content
                    archive.addfile(member)
                else:
                    archive.addfile(member)
        return self.archive

    def valid_entries(self):
        return [('zen', tarfile.DIRTYPE, None), ('zen/zen', tarfile.REGTYPE, fake_elf()), ('zen/browser/chrome/icons/default/default128.png', tarfile.REGTYPE, b'icon'), ('zen/LICENSE.txt', tarfile.REGTYPE, b'upstream license')]

    def test_strict_debian_version(self):
        self.assertEqual(debian_version('1.23.4b'), '1.23.4b+masternaka1')
        self.assertEqual(debian_version('v1.23.4'), '1.23.4+masternaka1')
        for tag in ('latest', '../1.2', '1.2\n', '1.2;evil', 'v1', '1.2rc1'):
            with self.assertRaises(PackagingError):
                debian_version(tag)

    def test_safe_extraction_preserves_licenses_normalizes_modes_and_internal_links(self):
        archive = self.archive_with(self.valid_entries() + [('zen/zen-link', tarfile.SYMTYPE, 'zen')])
        root = safe_extract(archive, self.root / 'expanded')
        self.assertEqual((root / 'LICENSE.txt').read_bytes(), b'upstream license')
        self.assertEqual((root / 'zen').stat().st_mode & 0o7777, 0o755)
        self.assertEqual((root / 'zen-link').resolve(), (root / 'zen').resolve())
        self.assertEqual(elf_files(root), [root / 'zen'])

    def test_archive_rejects_paths_links_special_files_and_duplicates(self):
        attacks = [
            [('outside', tarfile.REGTYPE, b'bad')],
            [('/zen/file', tarfile.REGTYPE, b'bad')],
            [('zen/../escape', tarfile.REGTYPE, b'bad')],
            [('zen/file', tarfile.LNKTYPE, 'zen/zen')],
            [('zen/file', tarfile.CHRTYPE, None)],
            [('zen/file', tarfile.SYMTYPE, '/etc/passwd')],
            [('zen/file', tarfile.SYMTYPE, '../../escape')],
            [('zen/sub', tarfile.SYMTYPE, '.'), ('zen/sub/file', tarfile.REGTYPE, b'bad')],
            [('zen/a', tarfile.SYMTYPE, 'b'), ('zen/b', tarfile.SYMTYPE, 'a')],
            [('zen/zen', tarfile.REGTYPE, b'duplicate')],
        ]
        for number, attack in enumerate(attacks):
            with self.subTest(attack=attack):
                self.archive_with(self.valid_entries() + attack)
                with self.assertRaises(PackagingError):
                    safe_extract(self.archive, self.root / f'expanded-{number}')

    def test_archive_limits(self):
        archive = self.archive_with(self.valid_entries())
        with self.assertRaises(PackagingError):
            safe_extract(archive, self.root / 'too-many', max_members=2)
        with self.assertRaises(PackagingError):
            safe_extract(archive, self.root / 'too-large', max_expanded=4)

    def test_wrong_elf_machine_and_missing_binary_rejected(self):
        self.archive_with([('zen/zen', tarfile.REGTYPE, fake_elf(183))])
        root = safe_extract(self.archive, self.root / 'wrong-elf')
        with self.assertRaises(PackagingError):
            elf_files(root)
        empty = self.root / 'empty'
        empty.mkdir()
        with self.assertRaises(PackagingError):
            elf_files(empty)

    def test_policy_merge_preserves_other_settings_and_refuses_symlink(self):
        root = self.root / 'tree'
        (root / 'distribution').mkdir(parents=True)
        policy = root / 'distribution/policies.json'
        policy.write_text(json.dumps({'extra': 'kept', 'policies': {'OtherSetting': True, 'DisableAppUpdate': False}}))
        merge_policies(root)
        self.assertEqual(json.loads(policy.read_text()), {'extra': 'kept', 'policies': {'OtherSetting': True, 'DisableAppUpdate': True}})
        policy.unlink()
        policy.symlink_to('../outside')
        with self.assertRaises(PackagingError):
            merge_policies(root)

    def test_builder_layout_launcher_policies_apparmor_and_ownership_without_dpkg(self):
        self.archive_with(self.valid_entries())
        target = self.root / 'zen-browser_1.23.4b+masternaka1_amd64.deb'
        def fake_run(command, **kwargs):
            self.assertEqual(command[:6], ['dpkg-deb', '--root-owner-group', '-Zxz', '-z6', '--threads-max=1', '--build'])
            self.assertEqual({key: kwargs['env'][key] for key in ('SOURCE_DATE_EPOCH', 'LC_ALL', 'TZ')}, {'SOURCE_DATE_EPOCH': '0', 'LC_ALL': 'C', 'TZ': 'UTC'})
            stage = Path(command[6])
            self.assertEqual(stage.stat().st_mtime, 0)
            self.assertTrue(all(path.lstat().st_mtime == 0 for path in stage.rglob('*')))
            self.assertEqual((stage / 'opt/zen-browser/LICENSE.txt').read_bytes(), b'upstream license')
            self.assertEqual((stage / 'usr/bin/zen-browser').read_text(), '#!/bin/sh\nexec /opt/zen-browser/zen "$@"\n')
            self.assertIn('libc6 (>= 2.38)', (stage / 'DEBIAN/control').read_text())
            self.assertTrue(json.loads((stage / 'opt/zen-browser/distribution/policies.json').read_text())['policies']['DisableAppUpdate'])
            self.assertIn('/opt/zen-browser/{zen,zen-bin}', (stage / 'etc/apparmor.d/masternaka-zen-browser').read_text())
            self.assertNotIn('sysctl', (stage / 'DEBIAN/postinst').read_text())
            target.write_bytes(b'generated-deb')
        with patch('tools.package_zen.dependencies', return_value='libc6 (>= 2.38), apparmor'), patch('tools.package_zen.subprocess.run', side_effect=fake_run):
            version = build_package(self.archive, '1.23.4b', target)
        self.assertEqual(version, '1.23.4b+masternaka1')
        self.assertTrue(target.exists())

    @unittest.skipUnless(sys.platform == 'linux' and platform.machine() == 'x86_64' and shutil.which('dpkg-deb'), 'Real dpkg reproducibility check requires Linux amd64 and dpkg-deb; skipped on macOS')
    def test_real_dpkg_build_is_reproducible_across_construction_times(self):
        entries = self.valid_entries()
        entries[1] = ('zen/zen', tarfile.REGTYPE, Path('/bin/true').read_bytes())
        self.archive_with(entries)
        first, second = self.root / 'first.deb', self.root / 'second.deb'
        # Use a real harmless system ELF as archive data without executing it.
        # Dependency discovery has its own argv test; this checks actual dpkg bytes.
        with patch('tools.package_zen.dependencies', return_value='libc6, apparmor'):
            build_package(self.archive, '1.23.4b', first)
            time.sleep(2)
            build_package(self.archive, '1.23.4b', second)
        self.assertEqual(first.read_bytes(), second.read_bytes())

    def test_shlibdeps_receives_one_library_directory_per_option(self):
        root = self.root / 'elf-tree'
        (root / 'private').mkdir(parents=True)
        (root / 'zen').write_bytes(fake_elf())
        (root / 'private/libexample.so').write_bytes(fake_elf())
        result = Mock(stdout='shlibs:Depends=libc6 (>= 2.38)\n', stderr='')
        with patch('tools.package_zen.subprocess.run', return_value=result) as run:
            self.assertEqual(dependencies(root, self.root), 'libc6 (>= 2.38), ca-certificates, apparmor')
        command = run.call_args.args[0]
        options = [part for part in command if part.startswith('-l')]
        self.assertEqual(options, ['-l' + str(root), '-l' + str(root / 'private')])
        self.assertTrue(all(':' not in part for part in options))
        self.assertEqual(sum(call.args[0][0] == 'readelf' for call in run.call_args_list), 2)

    def test_recipe_allowlist_and_two_source_provenance(self):
        config = self.root / 'apps.json'
        direct = {'repository': 'Masternaka/MintTweaks', 'package': 'minttweaks', 'architecture': 'all'}
        zen = {'repository': 'zen-browser/desktop', 'package': 'zen-browser', 'architecture': 'amd64', 'recipe': 'zen-tar'}
        config.write_text(json.dumps({'apps': [direct, zen]}))
        data = {'minttweaks_0.1.2_all.deb': b'direct-deb', 'zen.linux-x86_64.tar.xz': b'upstream-archive'}
        def fetch(url, **kwargs):
            is_zen = '/zen-browser/desktop/' in url
            name = 'zen.linux-x86_64.tar.xz' if is_zen else 'minttweaks_0.1.2_all.deb'
            if not kwargs.get('api'):
                return data[name]
            repo = 'zen-browser/desktop' if is_zen else 'Masternaka/MintTweaks'
            tag = '1.23.4b' if is_zen else 'v0.1.2'
            return json.dumps({'draft': False, 'prerelease': False, 'tag_name': tag, 'assets': [{'name': name, 'digest': 'sha256:' + hashlib.sha256(data[name]).hexdigest(), 'browser_download_url': f'https://github.com/{repo}/releases/download/{tag}/{name}'}]}).encode()
        def builder(archive, tag, output):
            self.assertEqual(archive.read_bytes(), b'upstream-archive')
            output.write_bytes(b'generated-deb')
        def identity(path):
            return {'Package': 'zen-browser', 'Architecture': 'amd64', 'Version': '1.23.4b+masternaka1'} if path.name.startswith('zen-browser') else {'Package': 'minttweaks', 'Architecture': 'all', 'Version': '0.1.2'}
        output = self.root / 'collected'
        manifest = collect(config, output, fetch=fetch, identity=identity, builder=builder)
        self.assertEqual(manifest[1]['asset'], 'zen.linux-x86_64.tar.xz')
        self.assertEqual(manifest[1]['digest'], 'sha256:' + hashlib.sha256(b'upstream-archive').hexdigest())
        self.assertEqual(manifest[1]['deb']['digest'], 'sha256:' + hashlib.sha256(b'generated-deb').hexdigest())
        self.assertNotEqual(manifest[1]['digest'], manifest[1]['deb']['digest'])
        self.assertFalse((output / 'zen.linux-x86_64.tar.xz').exists())
        self.assertEqual(manifest[0]['recipe'], 'deb')
        for invalid in ({**zen, 'repository': 'other/desktop'}, {**zen, 'package': 'other'}, {**zen, 'architecture': 'all'}, {**zen, 'recipe': 'shell'}):
            config.write_text(json.dumps({'apps': [invalid]}))
            with self.assertRaises(CollectionError):
                load_config(config)


if __name__ == '__main__':
    unittest.main()
