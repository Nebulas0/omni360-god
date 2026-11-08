"""Fault-injected public downloads and real release-signature verification."""
import hashlib
import io
import json
from pathlib import Path
import subprocess
import tempfile
import time
import unittest
from unittest.mock import Mock, patch
import urllib.error
import urllib.request

import wait_for_release as release


class Reply(io.BytesIO):
    status = 200


class DownloadTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        self.data = b'XEX2 synthetic download bytes'
        self.asset = {'name': 'Omni360.xex', 'id': 123, 'size': len(self.data),
            'url': 'https://api.github.com/repos/' + release.REPO + '/releases/assets/123',
            'browser_download_url': 'https://github.com/' + release.REPO + '/releases/download/vtest/Omni360.xex',
            'digest': 'sha256:' + hashlib.sha256(self.data).hexdigest()}

    def test_api_first_and_no_credentials(self):
        opener = Mock(); opener.open.return_value = Reply(self.data)
        with patch.object(release.urllib.request, 'build_opener', return_value=opener), patch.dict(release.os.environ, {'GITHUB_TOKEN': 'must-never-be-sent'}):
            release.download_asset(self.asset, self.root, time.monotonic() + 60)
        request = opener.open.call_args.args[0]
        self.assertEqual(request.full_url, self.asset['url'])
        self.assertEqual(request.get_header('Accept'), 'application/octet-stream')
        self.assertFalse({'authorization', 'cookie'} & {k.lower() for k, _ in request.header_items()})
        self.assertNotIn('must-never-be-sent', str(request.header_items()))
        self.assertEqual((self.root / 'Omni360.xex').read_bytes(), self.data)
        self.assertFalse(list(self.root.glob('*.partial')))

    def test_timeout_and_truncation_retry_new_routes(self):
        opener = Mock(); opener.open.side_effect = [TimeoutError(), Reply(b'XEX2'), Reply(self.data)]
        with patch.object(release.urllib.request, 'build_opener', return_value=opener), patch.object(release.time, 'sleep'):
            release.download_asset(self.asset, self.root, time.monotonic() + 60)
        requests = [c.args[0] for c in opener.open.call_args_list]
        self.assertEqual([r.full_url for r in requests], [self.asset['url'], self.asset['browser_download_url'], self.asset['url']])
        self.assertTrue(all(not r.has_header('Authorization') for r in requests))
        self.assertEqual((self.root / 'Omni360.xex').read_bytes(), self.data)

    def test_repeated_failure_never_replaces_previous_complete_file(self):
        path = self.root / 'Omni360.xex'; path.write_bytes(b'previous complete')
        opener = Mock(); opener.open.side_effect = TimeoutError()
        with patch.object(release.urllib.request, 'build_opener', return_value=opener), patch.object(release.time, 'sleep'):
            with self.assertRaises(TimeoutError): release.download_asset(self.asset, self.root, time.monotonic() + 60)
        self.assertEqual(opener.open.call_count, 4)
        self.assertEqual(path.read_bytes(), b'previous complete')
        self.assertFalse(list(self.root.glob('*.partial')))

    def test_bad_digest_fails_closed(self):
        opener = Mock(); opener.open.return_value = Reply(b'?' * len(self.data))
        with patch.object(release.urllib.request, 'build_opener', return_value=opener):
            with self.assertRaisesRegex(ValueError, 'checksum'): release.download_asset(self.asset, self.root, time.monotonic() + 60)
        self.assertEqual(opener.open.call_count, 1)
        self.assertFalse((self.root / 'Omni360.xex').exists())

    def test_rejects_foreign_urls_oversize_paths_and_expired_deadline(self):
        cases = [{'url': 'https://foreign.invalid/asset'}, {'size': release.MAX_ASSET + 1},
                 {'size': True}, {'name': '../outside'}, {'id': False}, {'digest': 'invalid'},
                 {'browser_download_url': 'http://github.com/' + release.REPO + '/releases/download/vtest/x'}]
        with patch.object(release.urllib.request, 'build_opener') as build:
            for changes in cases:
                with self.subTest(changes=changes), self.assertRaises(ValueError):
                    release.download_asset({**self.asset, **changes}, self.root, time.monotonic() + 60)
            build.assert_not_called()
        with patch.object(release.urllib.request, 'build_opener') as build:
            with self.assertRaises(TimeoutError): release.download_asset(self.asset, self.root, time.monotonic() - 1)
            build.return_value.open.assert_not_called()

    def test_redirect_stays_https_and_carries_no_credentials(self):
        redirect = release.PublicRedirect()
        request = urllib.request.Request(self.asset['url'], headers={'Authorization': 'should-be-stripped', 'Cookie': 'also-stripped'})
        new = redirect.redirect_request(request, None, 302, 'Found', {}, 'https://release-assets.githubusercontent.com/download')
        self.assertFalse(new.has_header('Authorization')); self.assertFalse(new.has_header('Cookie'))
        for url in ('http://foreign.invalid/file', 'https://user:password@foreign.invalid/file'):
            with self.assertRaises(ValueError): redirect.redirect_request(request, None, 302, 'Found', {}, url)


class VerificationTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        private = self.root / 'ephemeral-test-key.pem'
        subprocess.run(['openssl', 'ecparam', '-name', 'prime256v1', '-genkey', '-noout', '-out', str(private)], check=True, capture_output=True)
        der = subprocess.check_output(['openssl', 'ec', '-in', str(private), '-pubout', '-outform', 'DER'], stderr=subprocess.DEVNULL)
        self.header = self.root / 'UpdateKey.h'
        self.header.write_text('unsigned char kUpdatePublicKey[] = {' + ','.join('0x%02x' % n for n in der[-65:]) + '};')
        xex = self.root / 'Omni360.xex'; xex.write_bytes(b'XEX2' + b'synthetic signature fixture' * 10)
        subprocess.run(['openssl', 'dgst', '-sha256', '-sign', str(private), '-out', str(self.root / 'Omni360.xex.sig'), str(xex)], check=True, capture_output=True)
        self.report = {'bytes': xex.stat().st_size, 'sha256': hashlib.sha256(xex.read_bytes()).hexdigest(), 'xex_loader': {'test': 'loader'}}
        (self.root / 'build.json').write_text(json.dumps(self.report))
        self.sums()
        # Real XEX loader bounds are tested separately by test_builder. These
        # cases exercise the actual hashes, checksum-list and OpenSSL checks.
        p = patch.object(release, 'inspect_xex', return_value={'test': 'loader'})
        p.start(); self.addCleanup(p.stop)

    def sums(self):
        (self.root / 'SHA256SUMS').write_text(''.join(hashlib.sha256((self.root / n).read_bytes()).hexdigest() + '  ' + n + '\n' for n in release.ASSETS if n != 'SHA256SUMS'))

    def test_valid_signed_release(self):
        self.assertEqual(release.verify(self.root, self.header), self.report)

    def test_checksum_list_cannot_omit_or_repeat_files(self):
        original = (self.root / 'SHA256SUMS').read_text()
        for changed in (original.splitlines()[0] + '\n', original + original.splitlines()[0] + '\n', original + 'bad entry\n'):
            (self.root / 'SHA256SUMS').write_text(changed)
            with self.assertRaisesRegex(RuntimeError, 'checksum'): release.verify(self.root, self.header)

    def test_bad_size_or_signature_is_never_accepted(self):
        self.report['bytes'] += 1
        (self.root / 'build.json').write_text(json.dumps(self.report)); self.sums()
        with self.assertRaisesRegex(RuntimeError, 'size/loader'): release.verify(self.root, self.header)
        self.report['bytes'] -= 1
        xex = self.root / 'Omni360.xex'; xex.write_bytes(xex.read_bytes() + b'tampered')
        self.report.update(bytes=xex.stat().st_size, sha256=hashlib.sha256(xex.read_bytes()).hexdigest())
        (self.root / 'build.json').write_text(json.dumps(self.report)); self.sums()
        with self.assertRaises(subprocess.CalledProcessError): release.verify(self.root, self.header)


if __name__ == '__main__': unittest.main()
