"""Trusted host-side build/publish worker. Never runs code from PRs or forks.

The Wine container receives neither this worker's token nor the signing key.
Only main commits passing the named GitHub check are compiled. Each commit
gets a unique prerelease version; existing published releases are immutable.
"""
import argparse
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.parse
import urllib.request

REPO = 'Nebulas0/omni360-god'
API = 'https://api.github.com/repos/' + REPO
CHECK = 'Foundry protocol and server checks'


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs): return None


class GitHub:
    def __init__(self, token):
        self.token = token
        self.http = urllib.request.build_opener(NoRedirect)

    def request(self, path, value=None, method=None, data=None, content_type='application/json'):
        url = API + path if path.startswith('/') else path
        if urllib.parse.urlsplit(url).hostname not in {'api.github.com', 'uploads.github.com'}:
            raise ValueError('Refusing to send a credential to another host')
        if value is not None: data = json.dumps(value).encode()
        headers = {'Authorization': 'Bearer ' + self.token, 'Accept': 'application/vnd.github+json',
                   'X-GitHub-Api-Version': '2022-11-28', 'User-Agent': 'Omni360-Foundry-builder', 'Content-Type': content_type}
        request = urllib.request.Request(url, data=data, headers=headers, method=method)
        with self.http.open(request, timeout=90) as response:
            raw = response.read()
            return json.loads(raw) if raw else None

    def optional(self, path):
        try: return self.request(path)
        except urllib.error.HTTPError as e:
            if e.code == 404: return None
            raise


def run(*args, **kwargs):
    return subprocess.check_output(list(args), text=True, **kwargs).strip()


def write_json(path, value):
    temporary = path.with_suffix('.tmp')
    temporary.write_text(json.dumps(value, indent=2) + '\n')
    temporary.replace(path)


def sign(xex, signature, key, header):
    der = subprocess.check_output(['openssl', 'ec', '-in', str(key), '-pubout', '-outform', 'DER'], stderr=subprocess.DEVNULL)
    text = header.read_text().split('kUpdatePublicKey', 1)[1].split('{', 1)[1].split('}', 1)[0]
    expected = bytes(int(v, 16) for v in re.findall(r'0x([0-9a-fA-F]{2})', text))
    if len(expected) != 65 or der[-65:] != expected: raise RuntimeError('Release signing key differs from the compiled public key')
    subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(key), '-out', str(signature), str(xex)], check=True)
    public = signature.with_suffix('.public.pem')
    public.write_bytes(subprocess.check_output(['openssl', 'ec', '-in', str(key), '-pubout'], stderr=subprocess.DEVNULL))
    try:
        subprocess.run(['openssl', 'dgst', '-sha256', '-verify', str(public), '-signature', str(signature), str(xex)], check=True)
    finally: public.unlink(missing_ok=True)


def process(config, github, status):
    base = Path(config['base'])
    source, build, releases = [base / n for n in ('source', 'build', 'releases')]
    commit = github.request('/commits/main')['sha']
    if not re.fullmatch(r'[0-9a-f]{40}', commit): raise RuntimeError('Invalid commit')
    state = json.loads(status.read_text()) if status.exists() else {}
    if state.get('source_commit') == commit and state.get('state') == 'published': return state
    checks = github.request('/actions/runs?head_sha=' + commit + '&per_page=100')['workflow_runs']
    checks = [c for c in checks if c['name'] == CHECK and c['head_branch'] == 'main' and c['head_sha'] == commit]
    if not checks or checks[0]['status'] != 'completed' or checks[0]['conclusion'] != 'success':
        return {'state': 'waiting_for_checks', 'source_commit': commit}
    if not (source / '.git').is_dir():
        source.mkdir(parents=True, exist_ok=True)
        run('git', 'init', str(source))
        run('git', '-C', str(source), 'remote', 'add', 'origin', 'https://github.com/' + REPO + '.git')
    run('git', '-C', str(source), 'fetch', 'origin', 'main', '--no-tags')
    run('git', '-C', str(source), 'checkout', '--force', '--detach', commit)
    count = int(run('git', '-C', str(source), 'rev-list', '--count', commit))
    version = re.search(r'^#define CURRENT_VERSION "([^"]+)"', (source / 'src/settings.h').read_text(), re.M).group(1)
    if not re.fullmatch(r'\d+\.\d+\.\d+-(?:alpha|beta)\.[0-9]+', version):
        raise RuntimeError('Automatic releases require an alpha.N or beta.N base version')
    version += '.build.' + str(count)
    if len(version) >= 32: raise RuntimeError('Version exceeds the Xbox updater field')
    tag = 'v' + version
    existing = github.optional('/releases/tags/' + tag)
    if existing and not existing['draft']:
        if existing['target_commitish'] != commit: raise RuntimeError('Published version belongs to a different commit; never overwrite it')
        return {'state': 'published', 'source_commit': commit, 'version': version, 'url': existing['html_url']}
    write_json(status, {'state': 'building', 'source_commit': commit, 'version': version})
    # Update only the build image inputs from the checked main commit. SDK,
    # signing material, live application and the trusted publisher are excluded.
    deployed = Path(config['compose_dir']) / 'builder'
    for name in ('Dockerfile.wine', 'build.py', 'extract_sdk.py'):
        shutil.copy2(source / 'builder' / name, deployed / name)
    with (build / 'image-build.log').open('w') as out:
        subprocess.run(['docker', 'compose', '--profile', 'builder', 'build', 'xbox360-xex-builder'], cwd=config['compose_dir'], stdout=out, stderr=subprocess.STDOUT, check=True, timeout=1800)
        subprocess.run(['docker', 'compose', '--profile', 'builder', 'up', '-d', '--no-deps', '--no-build', 'xbox360-xex-builder'], cwd=config['compose_dir'], stdout=out, stderr=subprocess.STDOUT, check=True, timeout=120)
    # The source bind mount must keep the same inode; checkout modifies in place.
    log = build / 'native-build.log'
    with log.open('w') as out:
        subprocess.run(['docker', 'exec', 'xbox360-xex-builder', 'xvfb-run', '-a', 'python3', '/opt/builder/build.py', '--version', version], stdout=out, stderr=subprocess.STDOUT, check=True, timeout=3600)
    report = json.loads((build / 'Release/build.json').read_text())
    xex = build / 'Release/Omni360.xex'
    sha = hashlib.sha256(xex.read_bytes()).hexdigest()
    if report['version'] != version or report['sha256'] != sha or xex.read_bytes()[:4] != b'XEX2':
        raise RuntimeError('Build output does not match this release')
    target = releases / tag
    if target.exists():
        old = target / 'Omni360.xex'
        if old.exists() and hashlib.sha256(old.read_bytes()).hexdigest() != sha:
            # An interrupted unsigned/draft build may be retried. Never mutate published assets.
            if existing and not existing['draft']: raise RuntimeError('Immutable release conflict')
    target.mkdir(parents=True, exist_ok=True)
    final = target / 'Omni360.xex'
    shutil.copy2(xex, final)
    signature = target / 'Omni360.xex.sig'
    sign(final, signature, Path(config['signing_key']), source / 'src/UpdateKey.h')
    report.update(source_commit=commit, repository=REPO, image=run('docker', 'inspect', 'xbox360-xex-builder', '--format', '{{.Image}}'), checks_url=checks[0]['html_url'])
    write_json(target / 'build.json', report)
    (target / 'SHA256SUMS').write_text(''.join(hashlib.sha256((target / n).read_bytes()).hexdigest() + '  ' + n + '\n' for n in ('Omni360.xex', 'Omni360.xex.sig', 'build.json')))
    ref = github.optional('/git/ref/tags/' + tag)
    if ref:
        if ref['object']['sha'] != commit: raise RuntimeError('Existing tag has another commit')
    else: github.request('/git/refs', {'ref': 'refs/tags/' + tag, 'sha': commit})
    body = ('Automated Foundry alpha test build. Physical Xbox acceptance is still required.\n\n'
            'Source: `' + commit + '`\n\nSHA-256: `' + sha + '`\n\n'
            'Contains the Foundry service integration and download/install recovery fixes. '
            'The first install must be copied to the console manually; subsequent updates '
            'are available through Settings > Updates on this fork.\n\nChecks: ' + checks[0]['html_url'])
    release = existing or github.request('/releases', {'tag_name': tag, 'target_commitish': commit, 'name': 'Omni360 GoD ' + version, 'body': body, 'draft': True, 'prerelease': True})
    allowed = ('Omni360.xex', 'Omni360.xex.sig', 'build.json', 'SHA256SUMS')
    # Draft retries may replace only our known asset names; published files never change.
    for asset in release.get('assets', []):
        if asset['name'] in allowed: github.request('/releases/assets/' + str(asset['id']), method='DELETE')
    for name in allowed:
        github.request('https://uploads.github.com/repos/' + REPO + '/releases/' + str(release['id']) + '/assets?name=' + urllib.parse.quote(name),
                       data=(target / name).read_bytes(), method='POST', content_type='application/octet-stream')
    release = github.request('/releases/' + str(release['id']), {'draft': False, 'prerelease': True, 'make_latest': 'false'}, method='PATCH')
    return {'state': 'published', 'source_commit': commit, 'version': version, 'sha256': sha, 'url': release['html_url']}


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--config', type=Path, required=True)
    args = p.parse_args()
    config = json.loads(args.config.read_text())
    root = Path(config['base']) / 'automation'
    root.mkdir(exist_ok=True)
    with (root / 'worker.lock').open('w') as lock:
        try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError: return
        github = GitHub(Path(config['token_file']).read_text().strip())
        status = root / 'status.json'
        try:
            result = process(config, github, status)
            write_json(status, result)
            print(json.dumps(result), flush=True)
        except Exception as exc:
            previous = json.loads(status.read_text()) if status.exists() else {}
            previous.update(state='failed', error=type(exc).__name__ + ': ' + str(exc)[:500])
            write_json(status, previous)
            raise


if __name__ == '__main__': main()
