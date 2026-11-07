"""GitHub Actions waits for the private ZenServer build, then verifies artifacts."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import tempfile
import time
import urllib.request
from publish import GitHub


def verify(directory, header):
    report = json.loads((directory / 'build.json').read_text())
    xex = directory / 'Omni360.xex'
    if xex.read_bytes()[:4] != b'XEX2' or hashlib.sha256(xex.read_bytes()).hexdigest() != report['sha256']:
        raise RuntimeError('Downloaded XEX checksum/format mismatch')
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
            args.output.mkdir(parents=True, exist_ok=True)
            for name in ('Omni360.xex', 'Omni360.xex.sig', 'build.json', 'SHA256SUMS'):
                # Public asset downloads receive no API credential, even on redirects.
                url = assets[name]['browser_download_url']
                if not url.startswith('https://github.com/Nebulas0/omni360-god/releases/download/'):
                    raise RuntimeError('Unexpected release download origin')
                with urllib.request.urlopen(url, timeout=90) as response:
                    data = response.read(32 * 1024 * 1024 + 1)
                if len(data) > 32 * 1024 * 1024: raise RuntimeError('Release asset exceeds size limit')
                (args.output / name).write_bytes(data)
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
