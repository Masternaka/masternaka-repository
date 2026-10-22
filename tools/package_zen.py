#!/usr/bin/env python3
"""Build an unofficial Debian package from the allowlisted Zen Linux archive.

Never execute upstream code: inspect ELF with readelf and dpkg-shlibdeps only.
"""
import json
import os
from pathlib import Path, PurePosixPath
import re
import shutil
import struct
import subprocess
import tarfile
import tempfile


class PackagingError(ValueError):
    pass


# Bump whenever a recipe or Ubuntu baseline change alters bytes for one tag.
RECIPE_REVISION = 1
MAX_MEMBERS = 20000
MAX_EXPANDED = 2 * 1024 * 1024 * 1024
PROFILE = '''abi <abi/4.0>,
include <tunables/global>
profile masternaka-zen-browser /opt/zen-browser/{zen,zen-bin} flags=(unconfined) {
  userns,
  include if exists <local/masternaka-zen-browser>
}
'''


def debian_version(tag):
    if not isinstance(tag, str) or not re.fullmatch(r'v?[0-9]+(?:\.[0-9]+)+b?', tag):
        raise PackagingError('Unsupported Zen release tag')
    return tag.removeprefix('v') + f'+masternaka{RECIPE_REVISION}'


def member_path(name):
    parts = name.rstrip('/').split('/')
    if not parts or parts[0] != 'zen' or any(p in ('', '.', '..') for p in parts):
        raise PackagingError('Archive entry must remain under zen/')
    return PurePosixPath(*parts)


def safe_extract(archive, destination, max_members=MAX_MEMBERS, max_expanded=MAX_EXPANDED):
    """Manually extract only regular files, dirs and nonescaping relative symlinks."""
    destination = Path(destination)
    destination.mkdir(mode=0o755, parents=True, exist_ok=False)
    with tarfile.open(archive, mode='r:xz') as source:
        entries = []
        seen = set()
        total = 0
        for member in source:
            if len(entries) >= max_members:
                raise PackagingError('Archive member limit exceeded')
            path = member_path(member.name)
            if path in seen:
                raise PackagingError('Duplicate archive entry')
            seen.add(path)
            if not (member.isdir() or member.isreg() or member.issym()):
                raise PackagingError('Unsupported archive entry type')
            total += member.size
            if member.size < 0 or total > max_expanded:
                raise PackagingError('Archive expanded size limit exceeded')
            if path == PurePosixPath('zen') and not member.isdir():
                raise PackagingError('Archive root must be a directory')
            if member.issym():
                link = member.linkname
                if not link or link.startswith('/'):
                    raise PackagingError('Unsafe absolute or empty symlink')
                resolved = list(path.parent.parts)
                for part in link.split('/'):
                    if part == '..':
                        if len(resolved) <= 1:
                            raise PackagingError('Symlink escapes archive root')
                        resolved.pop()
                    elif part not in ('', '.'):
                        resolved.append(part)
            entries.append((path, member))
        kinds = {path: member for path, member in entries}
        for path, _ in entries:
            for parent in path.parents:
                if parent in kinds and not kinds[parent].isdir():
                    raise PackagingError('Archive entry has a non-directory parent')
        # Write files before links, so archive links can never redirect writes.
        for path, member in entries:
            if member.issym():
                continue
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            if member.isdir():
                target.mkdir(mode=0o755, exist_ok=True)
            else:
                with source.extractfile(member) as content, target.open('xb') as output:
                    shutil.copyfileobj(content, output)
                target.chmod(0o755 if member.mode & 0o111 else 0o644)
        for path, member in entries:
            if not member.issym():
                continue
            target = destination.joinpath(*path.parts)
            target.parent.mkdir(mode=0o755, parents=True, exist_ok=True)
            target.symlink_to(member.linkname)
        root = (destination / 'zen').resolve()
        for path, member in entries:
            if member.issym():
                try:
                    target = destination.joinpath(*path.parts).resolve(strict=True)
                    target.relative_to(root)
                except (OSError, RuntimeError, ValueError) as error:
                    raise PackagingError('Symlink target missing, cyclic or outside zen/') from error
    return destination / 'zen'


def elf_files(root):
    files = []
    for path in sorted(root.rglob('*')):
        if path.is_file() and not path.is_symlink():
            with path.open('rb') as stream:
                header = stream.read(20)
            if header.startswith(b'\x7fELF'):
                if len(header) != 20 or header[4:6] != b'\x02\x01' or struct.unpack('<H', header[18:20])[0] != 62:
                    raise PackagingError('Zen archive contains a non-x86_64 ELF')
                files.append(path)
    if not any(path.name in ('zen', 'zen-bin') and path.parent == root for path in files):
        raise PackagingError('Missing x86_64 Zen executable')
    return files


def merge_policies(root):
    directory = root / 'distribution'
    if directory.is_symlink():
        raise PackagingError('Policy directory must not be a symlink')
    directory.mkdir(exist_ok=True)
    path = directory / 'policies.json'
    if path.is_symlink():
        raise PackagingError('Policy file must not be a symlink')
    config = json.loads(path.read_text()) if path.exists() else {}
    if not isinstance(config, dict) or not isinstance(config.get('policies', {}), dict):
        raise PackagingError('Unsupported upstream policies.json')
    config.setdefault('policies', {})['DisableAppUpdate'] = True
    path.write_text(json.dumps(config, indent=2) + '\n')
    path.chmod(0o644)


def dependencies(root, work):
    files = elf_files(root)
    for path in files:
        subprocess.run(['readelf', '--dynamic', str(path)], check=True, capture_output=True, text=True)
    directories = sorted({str(path.parent) for path in files})
    result = subprocess.run(['dpkg-shlibdeps', '-O', '--ignore-missing-info'] + ['-l' + directory for directory in directories] + ['-e' + str(path) for path in files], cwd=work, check=True, capture_output=True, text=True, env={**os.environ, 'LC_ALL': 'C'})
    if result.stderr:
        print(result.stderr, end='')
    lines = [line.partition('=')[2] for line in result.stdout.splitlines() if line.startswith('shlibs:Depends=')]
    if len(lines) != 1 or not lines[0].strip():
        raise PackagingError('Could not derive Zen shared-library dependencies')
    # Runtime services not represented by ELF NEEDED records.
    print('Zen ELF-derived dependencies: ' + lines[0])
    return lines[0] + ', ca-certificates, apparmor'


def maintainer_scripts(debian):
    postinst = '''#!/bin/sh
set -e
if [ "$1" = configure ] && [ -d /sys/kernel/security/apparmor ] && [ -x /sbin/apparmor_parser ]; then
  /sbin/apparmor_parser -r /etc/apparmor.d/masternaka-zen-browser || { echo "Unable to load Zen AppArmor profile" >&2; exit 1; }
fi
exit 0
'''
    postrm = '''#!/bin/sh
set -e
case "$1" in
  remove|purge)
    if [ -d /sys/kernel/security/apparmor ] && [ -x /sbin/apparmor_parser ] && [ -f /etc/apparmor.d/masternaka-zen-browser ]; then
      /sbin/apparmor_parser -R /etc/apparmor.d/masternaka-zen-browser || true
    fi
    ;;
esac
exit 0
'''
    # prerm removes the loaded profile while its file still exists; postrm is
    # a fallback for interrupted removals and never changes global userns policy.
    prerm = postrm.replace('remove|purge)', 'remove|deconfigure)')
    for name, text in (('postinst', postinst), ('postrm', postrm), ('prerm', prerm)):
        (debian / name).write_text(text)
        (debian / name).chmod(0o755)


def build_package(archive, tag, output):
    version = debian_version(tag)
    output = Path(output)
    output.parent.mkdir(parents=True, exist_ok=True)
    if output.exists():
        raise PackagingError('Zen output package already exists')
    try:
        with tempfile.TemporaryDirectory(prefix='package-zen-') as temporary:
            work = Path(temporary)
            root = safe_extract(archive, work / 'extracted')
            icon = root / 'browser/chrome/icons/default/default128.png'
            if not icon.is_file():
                raise PackagingError('Missing upstream Zen desktop icon')
            executables = elf_files(root)
            executable = 'zen' if root / 'zen' in executables else 'zen-bin'
            merge_policies(root)
            (work / 'debian').mkdir()
            (work / 'debian/control').write_text('Source: zen-browser\nSection: web\nPriority: optional\nMaintainer: Masternaka repository <noreply@masternaka.invalid>\n\nPackage: zen-browser\nArchitecture: amd64\nDescription: unofficial Zen Browser archive repackaging\n')
            depends = dependencies(root, work)
            stage = work / 'stage'
            (stage / 'opt').mkdir(parents=True)
            shutil.move(str(root), stage / 'opt/zen-browser')
            for directory in ('DEBIAN', 'usr/bin', 'usr/share/applications', 'usr/share/icons/hicolor/128x128/apps', 'etc/apparmor.d', 'usr/share/doc/zen-browser'):
                (stage / directory).mkdir(parents=True, exist_ok=True)
            (stage / 'DEBIAN/control').write_text(f'Package: zen-browser\nVersion: {version}\nArchitecture: amd64\nMaintainer: Masternaka repository <noreply@masternaka.invalid>\nSection: web\nPriority: optional\nDepends: {depends}\nDescription: unofficial Zen Browser archive repackaging\n Complete upstream Linux archive, distributed through the Masternaka APT repository.\n')
            launcher = stage / 'usr/bin/zen-browser'
            launcher.write_text(f'#!/bin/sh\nexec /opt/zen-browser/{executable} "$@"\n')
            launcher.chmod(0o755)
            (stage / 'usr/share/applications/zen-browser.desktop').write_text('[Desktop Entry]\nType=Application\nName=Zen Browser\nExec=zen-browser %u\nIcon=zen-browser\nTerminal=false\nCategories=Network;WebBrowser;\nMimeType=text/html;x-scheme-handler/http;x-scheme-handler/https;\n')
            shutil.copyfile(stage / 'opt/zen-browser/browser/chrome/icons/default/default128.png', stage / 'usr/share/icons/hicolor/128x128/apps/zen-browser.png')
            (stage / 'etc/apparmor.d/masternaka-zen-browser').write_text(PROFILE)
            (stage / 'DEBIAN/conffiles').write_text('/etc/apparmor.d/masternaka-zen-browser\n')
            (stage / 'usr/share/doc/zen-browser/copyright').write_text(f'Unofficial repackaging by the Masternaka repository.\nUpstream: https://github.com/zen-browser/desktop/releases/tag/{tag}\nSource asset: zen.linux-x86_64.tar.xz\nThe complete upstream tree and its license notices are preserved under /opt/zen-browser.\nConsult upstream about:license and source files for license details.\n')
            maintainer_scripts(stage / 'DEBIAN')
            # Strip source and construction timestamps from every archive member.
            for path in stage.rglob('*'):
                if not path.is_symlink():
                    path.chmod(0o755 if path.is_dir() or path.stat().st_mode & 0o111 else 0o644)
                os.utime(path, (0, 0), follow_symlinks=False)
            stage.chmod(0o755)
            os.utime(stage, (0, 0), follow_symlinks=False)
            subprocess.run(['dpkg-deb', '--root-owner-group', '-Zxz', '-z6', '--threads-max=1', '--build', str(stage), str(output)], check=True, capture_output=True, text=True, env={**os.environ, 'SOURCE_DATE_EPOCH': '0', 'LC_ALL': 'C', 'TZ': 'UTC'})
    except (OSError, subprocess.CalledProcessError, tarfile.TarError, ValueError) as error:
        output.unlink(missing_ok=True)
        raise PackagingError(f'Zen packaging failed: {error}') from error
    return version
