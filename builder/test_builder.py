"""SDK-free regression tests for project translation and release safeguards."""
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import struct
import tempfile
import unittest
from unittest.mock import patch

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent


def load(name):
    spec = importlib.util.spec_from_file_location(name, HERE / (name + '.py'))
    module = importlib.util.module_from_spec(spec); spec.loader.exec_module(module)
    return module


build, publish, extract = load('build'), load('publish'), load('extract_sdk')


class BuilderTests(unittest.TestCase):
    def test_loader_checks_reject_truncation_bad_entry_and_missing_fonts(self):
        # Synthetic header exercises bounds; real builds are independently
        # inspected by the SDK and by both release pipeline stages.
        data = bytearray(0x210)
        struct.pack_into('>6I', data, 0, 0x58455832, 1, 0x200, 0, 0x80, 6)
        headers = [(0x2FF, 0x130), (0x3FF, 0x180), (0x10100, 0x82000100), (0x10201, 0x82000000), (0x20200, 0x40000), (0x40006, 0x110)]
        for i, entry in enumerate(headers): struct.pack_into('>II', data, 24 + i * 8, *entry)
        struct.pack_into('>I', data, 0x84, 0x10000)
        struct.pack_into('>I', data, 0x11C, 0xFFFF0360)
        struct.pack_into('>I', data, 0x130, 52)
        for i, name in enumerate((b'font', b'selawk', b'ofl')):
            struct.pack_into('>8sII', data, 0x134 + i * 16, name, 0x82002000 + i * 16, 16)
        struct.pack_into('>IHHII', data, 0x180, 16, 1, 1, 16, 0xFFF0)
        self.assertEqual(build.inspect_xex(data)['title_id'], 'FFFF0360')
        for kind in ('truncated', 'entry', 'font', 'resource'):
            bad = bytearray(data)
            if kind == 'truncated': bad = bad[:-1]
            elif kind == 'entry': struct.pack_into('>I', bad, 44, 0x90000000)
            elif kind == 'font': bad[0x134:0x13C] = b'missing\0'
            else: struct.pack_into('>I', bad, 0x13C, 0x90000000)
            with self.subTest(kind=kind), self.assertRaises(ValueError): build.inspect_xex(bad)

    def test_actual_release_project_and_file_options(self):
        files, link = build.project(ROOT / 'XboxTLS2.vcxproj')
        atg, _ = build.project(ROOT / 'Common/AtgFramework2010.vcxproj')
        self.assertGreater(len(files), 350); self.assertGreater(len(atg), 40)
        names = {p.name for p, _ in files}
        self.assertTrue({'Foundry.cpp', 'FoundryProtocol.cpp', 'UpdateCheck.cpp'} <= names)
        rar = next(v for p, v in files if p.name == 'RarUnpack.cpp')
        includes = build.expand_list(rar['AdditionalIncludeDirectories'], ROOT)
        self.assertTrue(any('dmc_unrar/xbox' in p for p in includes))
        self.assertIn(str(ROOT / 'src'), includes)
        self.assertIn('AtgFramework.lib', link['AdditionalDependencies'])

    def test_sdk_manifest_copies_shared_headers_and_rejects_escape(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); preview = root / 'XDK/TechPreview/include'
            preview.mkdir(parents=True); (preview / 'excpt.h').write_bytes(b'test header')
            manifest = root / 'manifest.csv'
            manifest.write_bytes(b'0000,dev,copy,XDK,XDK\\TechPreview\\include\\excpt.h,XDK\\include\\xbox\\excpt.h,,2\r\n')
            self.assertEqual(extract.apply_manifest(root), 1)
            self.assertEqual((root / 'XDK/include/xbox/excpt.h').read_bytes(), b'test header')
            manifest.write_text('0000,dev,copy,XDK,XDK\\TechPreview\\include\\excpt.h,XDK\\..\\outside,,2\n')
            with self.assertRaises(ValueError): extract.apply_manifest(root)

    def test_authentication_never_sent_to_download_hosts(self):
        with self.assertRaises(ValueError):
            publish.GitHub('unused-test-value').request('https://example.invalid/upload', {})

    def test_published_release_is_not_replaced(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp); source = root / 'source'; (source / '.git').mkdir(parents=True)
            (source / 'src').mkdir(); (source / 'src/settings.h').write_text('#define CURRENT_VERSION "1.0.0-alpha.4"\n')
            commit = 'a' * 40
            class Fake:
                writes = []
                def request(self, path, *args, **kwargs):
                    if path == '/commits/main': return {'sha': commit}
                    if path.startswith('/actions/runs?'): return {'workflow_runs': [{'name': publish.CHECK, 'head_branch': 'main', 'head_sha': commit, 'status': 'completed', 'conclusion': 'success'}]}
                    self.writes.append(path); raise AssertionError('Unexpected mutation')
                def optional(self, path):
                    return {'draft': False, 'target_commitish': 'b' * 40, 'html_url': 'unused'}
            api = Fake()
            with patch.object(publish, 'run', side_effect=lambda *a, **k: '185' if 'rev-list' in a else ''):
                with self.assertRaisesRegex(RuntimeError, 'never overwrite'):
                    publish.process({'base': str(root)}, api, root / 'status.json')
            self.assertEqual(api.writes, [])

    def test_real_xbox_updater_orders_generated_versions(self):
        compiler = shutil.which('c++')
        self.assertIsNotNone(compiler)
        source = (ROOT / 'src/UpdateCheck.cpp').read_text()
        functions = source[source.index('static void SplitVersion'):source.index('// Release notes')]
        with tempfile.TemporaryDirectory() as tmp:
            cpp, binary = Path(tmp) / 'versions.cpp', Path(tmp) / 'versions'
            cpp.write_text('#include <stdlib.h>\n#include <string.h>\n#include <assert.h>\n' + functions + '''
int main() {
 assert(CompareVersions("1.0.0-alpha.4.build.186", "1.0.0-alpha.4") > 0);
 assert(CompareVersions("1.0.0-alpha.4.build.1000", "1.0.0-alpha.4.build.999") > 0);
 assert(CompareVersions("1.0.0-alpha.5.build.1", "1.0.0-alpha.4.build.999") > 0);
 return 0;
}
''')
            subprocess.run([compiler, '-std=c++03', str(cpp), '-o', str(binary)], check=True)
            subprocess.run([str(binary)], check=True)


if __name__ == '__main__': unittest.main()
