"""Сборка автономного .app с Developer ID на сборочном Mac."""
import datetime
import hashlib
import os
from pathlib import Path
import plistlib
import re
import shutil
import subprocess

IDENTIFIER = 'com.studio.agent'
CAPTURE = 'com.apple.developer.persistent-content-capture'
MACH = {bytes.fromhex(value) for value in
        ('feedface', 'cefaedfe', 'feedfacf', 'cffaedfe', 'cafebabe', 'bebafeca', 'cafebabf', 'bfbafeca')}


def signing_team(identity, listing, kind='Application'):
    pattern = rf'\s*\d+\)\s+([A-Fa-f0-9]{{40}})\s+"Developer ID {re.escape(kind)}: .+ \(([A-Z0-9]{{10}})\)"\s*'
    if re.fullmatch(r'[A-Fa-f0-9]{40}', identity):
        for line in listing.splitlines():
            match = re.fullmatch(pattern, line)
            if match and match[1].upper() == identity.upper():
                return match[2]
    raise ValueError(f'Нужен доступный Developer ID {kind} и его SHA-1 в Keychain сборщика')


def capture_entitlements(profile, team, identity, now):
    grants = profile.get('Entitlements', {})
    app_id = grants.get('com.apple.application-identifier')
    permitted_ids = {prefix + '.' + IDENTIFIER for prefix in profile.get('ApplicationIdentifierPrefix', [team])}
    if (team not in profile.get('TeamIdentifier', []) or profile.get('ExpirationDate') is None
            or profile['ExpirationDate'].replace(tzinfo=datetime.timezone.utc) <= now
            or app_id not in permitted_ids
            or grants.get('com.apple.developer.team-identifier') != team or grants.get(CAPTURE) is not True
            or not any(hashlib.sha1(cert).hexdigest().upper() == identity.upper()
                       for cert in profile.get('DeveloperCertificates', []))):
        raise ValueError('Профиль Apple не разрешает Persistent Content Capture для этого приложения и сертификата')
    return {'com.apple.application-identifier': app_id, 'com.apple.developer.team-identifier': team, CAPTURE: True}


def preflight(identity, profile):
    listing = subprocess.check_output(['/usr/bin/security', 'find-identity', '-v', '-p', 'codesigning'],
                                      text=True, timeout=20)
    team = signing_team(identity, listing)
    if profile:
        data = subprocess.check_output(['/usr/bin/security', 'cms', '-D', '-i', str(profile)], timeout=30)
        grants = capture_entitlements(plistlib.loads(data), team, identity, datetime.datetime.now(datetime.timezone.utc))
    else:
        grants = {}
    return team, grants


def sign(path, identity, entitlements=None):
    command = ['/usr/bin/codesign', '--force', '--timestamp', '--options', 'runtime', '--sign', identity]
    if entitlements is not None:
        command.extend(['--entitlements', str(entitlements), '--generate-entitlement-der'])
    subprocess.run([*command, str(path)], check=True, timeout=120, stdout=subprocess.DEVNULL)


def build_app(source, scripts, work, lock, template, version, identity, profile, team, grants, env):
    app = work / 'signed-scripts/Studio Agent.app'
    contents = app / 'Contents'
    resources = contents / 'Resources'
    binary = contents / 'MacOS/studio-agent'
    binary.parent.mkdir(parents=True)
    resources.mkdir()
    code = resources / 'agent/studio_agent'
    shutil.copytree(source / 'studio_agent', code, ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
    info = plistlib.loads((template / 'Contents/Info.plist').read_bytes())
    info.update(CFBundleVersion=version, CFBundleShortVersionString=version,
                StudioSigningTeam=team, StudioPersistentCapture=bool(grants))
    (contents / 'Info.plist').write_bytes(plistlib.dumps(info))
    icon = info['CFBundleIconFile']
    shutil.copyfile(template / 'Contents/Resources' / icon, resources / icon)
    native = scripts / 'runtime' / os.uname().machine / 'bin/python3'
    minor = '.'.join(lock['version'].split('.')[:2])
    slices = []
    for arch in ('arm64', 'x86_64'):
        runtime = resources / 'runtime' / arch
        shutil.copytree(scripts / 'runtime' / arch, runtime, symlinks=True,
                        ignore=shutil.ignore_patterns('__pycache__', '*.pyc'))
        site = runtime / 'lib' / ('python' + minor) / 'site-packages'
        subprocess.run([str(native), '-m', 'pip', 'install', '--no-index', '--no-compile', '--only-binary=:all:',
                        '--platform', 'macosx_13_0_' + arch, '--implementation', 'cp', '--python-version', minor,
                        '--abi', 'cp' + minor.replace('.', ''), '--target', str(site), '--upgrade',
                        '--find-links', str(scripts / 'wheels' / arch), '-r', str(work / (arch + '-requirements.txt')),
                        '-c', str(work / (arch + '-constraints.txt'))], env=env, check=True, timeout=600)
        def traversal_error(error):
            raise error

        for directory, folders, files in os.walk(runtime, onerror=traversal_error):
            for name in folders + files:
                path = Path(directory) / name
                if path.is_symlink():
                    if not path.resolve(strict=True).is_relative_to(runtime):
                        raise ValueError('Ссылка runtime выходит за пределы подписываемого каталога')
                    continue
                if path.is_dir():
                    continue
                if path == site / 'claude_agent_sdk/_bundled/claude':
                    # Отдельный процесс SDK использует собственные параметры подписи и JIT.
                    subprocess.run(['/usr/bin/codesign', '--verify', '--strict', str(path)], check=True, timeout=60)
                    continue
                with path.open('rb') as handle:
                    magic = handle.read(4)
                if magic in MACH:
                    sign(path, identity)
        part = work / ('sealed-' + arch)
        subprocess.run(['/usr/bin/clang', '-arch', arch, '-mmacosx-version-min=13.0', '-framework', 'Foundation',
                        '-framework', 'Security', '-I', str(runtime / 'include' / ('python' + minor)),
                        str(source / 'macos/sealed.m'), '-o', str(part)], env=env, check=True, timeout=120)
        slices.append(part)
    subprocess.run(['/usr/bin/lipo', '-create', *slices, '-output', binary], check=True, timeout=30)
    binary.chmod(0o755)
    rights = None
    if profile:
        shutil.copyfile(profile, contents / 'embedded.provisionprofile')
        rights = work / 'capture-entitlements.plist'
        rights.write_bytes(plistlib.dumps(grants))
    sign(app, identity, rights)
    return app
