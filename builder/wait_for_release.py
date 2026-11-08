"""GitHub Actions waits for the private ZenServer build, then verifies artifacts."""
import argparse
import hashlib
import http.client
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.request
import urllib.error
import urllib.parse
from publish import GitHub, REPO
from build import inspect_xex

ASSETS = ('Omni360.xex', 'Omni360.xex.sig', 'build.json', 'SHA256SUMS')
MAX_ASSET = 32 * 1024 * 1024


class PublicRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, request, fp, code, message, headers, newurl):
        parsed = urllib.parse.urlsplit(newurl)
        if parsed.scheme != 'https' or parsed.username or parsed.password:
            raise ValueError('Unsafe public asset redirect')
        redirected = super().redirect_request(request, fp, code, message, headers, newurl)
        if redirected is not None:
            redirected.remove_header('Authorization')
            redirected.remove_header('Cookie')
        return redirected


def download_asset(asset, directory, deadline):
    """Use GitHub's binary API first; retain a public browser-URL fallback.

    A new opener deliberately has no API credentials/cookies. Neither the
    initial request nor a redirected CDN request may receive GITHUB_TOKEN.
    """
    name, identifier, size = asset.get('name'), asset.get('id'), asset.get('size')
    if name not in ASSETS or type(identifier) is not int or identifier <= 0 or type(size) is not int or not 0 < size <= MAX_ASSET:
        raise ValueError('Invalid release asset metadata')
    api_url = 'https://api.github.com/repos/' + REPO + '/releases/assets/' + str(identifier)
    browser = asset.get('browser_download_url', '')
    if asset.get('url') != api_url or not browser.startswith('https://github.com/' + REPO + '/releases/download/'):
        raise ValueError('Unexpected release download origin')
    digest = asset.get('digest')
    if digest is not None and not re.fullmatch(r'sha256:[0-9a-fA-F]{64}', digest):
        raise ValueError('Invalid release asset digest')
    opener = urllib.request.build_opener(PublicRedirect())
    for attempt in range(4):
        left = deadline - time.monotonic()
        if left <= 0: raise TimeoutError('Release verification deadline reached')
        route = 'binary API' if attempt % 2 == 0 else 'public download'
        url = api_url if attempt % 2 == 0 else browser
        print(f'Downloading {name}: {route}, attempt {attempt + 1}/4', flush=True)
        request = urllib.request.Request(url, headers={'Accept': 'application/octet-stream',
            'Accept-Encoding': 'identity', 'User-Agent': 'Omni360-Release-Verifier'})
        try:
            with opener.open(request, timeout=min(20, left)) as response:
                if response.status != 200: raise ValueError('Unexpected asset response status')
                data = response.read(size + 1)
            if len(data) != size: raise http.client.IncompleteRead(data, size)
            if digest and hashlib.sha256(data).hexdigest() != digest.split(':', 1)[1].lower():
                raise ValueError('Asset checksum differs from GitHub release metadata')
        except (OSError, http.client.HTTPException) as exc:
            if isinstance(exc, urllib.error.HTTPError) and exc.code not in (403, 404, 408, 425, 429) and not 500 <= exc.code <= 599:
                raise
            # Don't log signed redirect URLs, request headers or credentials.
            print(f'{name}: {type(exc).__name__}; retrying a fresh request' if attempt < 3 else f'{name}: download failed after four attempts', flush=True)
            if attempt == 3: raise
            time.sleep(min(attempt + 1, max(0, deadline - time.monotonic())))
            continue
        temporary = directory / (name + '.partial')
        try:
            temporary.write_bytes(data)
            temporary.replace(directory / name)
        finally:
            temporary.unlink(missing_ok=True)
        print(f'Downloaded {name}: {len(data)} bytes', flush=True)
        return


def verify(directory, header):
    report = json.loads((directory / 'build.json').read_text())
    xex = directory / 'Omni360.xex'
    if xex.read_bytes()[:4] != b'XEX2' or hashlib.sha256(xex.read_bytes()).hexdigest() != report['sha256']:
        raise RuntimeError('Downloaded XEX checksum/format mismatch')
    loader = inspect_xex(xex.read_bytes())
    if report['bytes'] != xex.stat().st_size or report['xex_loader'] != loader:
        raise RuntimeError('Downloaded XEX size/loader differs from build metadata')
    sums = {}
    for line in (directory / 'SHA256SUMS').read_text().splitlines():
        match = re.fullmatch(r'([0-9a-fA-F]{64})  (Omni360\.xex(?:\.sig)?|build\.json)', line)
        if not match or match[2] in sums: raise RuntimeError('Invalid release checksum list')
        sums[match[2]] = match[1].lower()
    if set(sums) != set(ASSETS) - {'SHA256SUMS'}:
        raise RuntimeError('Release checksum list is incomplete')
    for name, expected in sums.items():
        if hashlib.sha256((directory / name).read_bytes()).hexdigest() != expected:
            raise RuntimeError('Release checksum mismatch: ' + name)
    body = header.read_text().split('kUpdatePublicKey', 1)[1].split('{', 1)[1].split('}', 1)[0]
    point = bytes(int(v, 16) for v in re.findall(r'0x([0-9a-fA-F]{2})', body))
    if len(point) != 65: raise RuntimeError('Invalid release public key')
    with tempfile.TemporaryDirectory() as tmp:
        public = Path(tmp) / 'public.der'
        public.write_bytes(bytes.fromhex('3059301306072a8648ce3d020106082a8648ce3d030107034200') + point)
        subprocess.run(['openssl', 'dgst', '-sha256', '-keyform', 'DER', '-verify', str(public), '-signature', str(directory / 'Omni360.xex.sig'), str(xex)], check=True)
    return report


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--commit', required=True)
    p.add_argument('--timeout', type=int, default=3600)
    p.add_argument('--output', type=Path, default=Path('Release'))
    args = p.parse_args()
    api = GitHub(os.environ['GITHUB_TOKEN'])
    root = Path(__file__).resolve().parents[1]
    version = re.search(r'^#define CURRENT_VERSION "([^"]+)"', (root / 'src/settings.h').read_text(), re.M).group(1)
    count = subprocess.check_output(['git', 'rev-list', '--count', args.commit], text=True).strip()
    tag = 'v' + version + '.build.' + count
    deadline = time.monotonic() + args.timeout
    print('Waiting for ZenServer Wine build ' + tag, flush=True)
    while time.monotonic() < deadline:
        release = api.optional('/releases/tags/' + tag)
        if release and not release['draft']:
            if release['target_commitish'] != args.commit: raise RuntimeError('Release points to another source commit')
            assets = {a['name']: a for a in release['assets']}
            if any(name not in assets or assets[name].get('state') != 'uploaded' for name in ASSETS):
                raise RuntimeError('Published release is missing a completed asset')
            args.output.mkdir(parents=True, exist_ok=True)
            for name in ASSETS:
                download_asset(assets[name], args.output, deadline)
            report = verify(args.output, root / 'src/UpdateKey.h')
            if report['source_commit'] != args.commit or report['version'] != tag[1:]:
                raise RuntimeError('Build metadata does not match this workflow')
            print(release['html_url'], flush=True)
            return
        if api.request('/commits/main')['sha'] != args.commit:
            print('Superseded by a newer main commit; the server builds the newest passing source.', flush=True)
            return
        time.sleep(30)
    raise SystemExit('ZenServer build did not publish in time. Check its builder status and native-build.log.')


if __name__ == '__main__': main()
