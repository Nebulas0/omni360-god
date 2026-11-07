"""Extract embedded CABs from a supplied XDK installer without executing it."""
import argparse
import csv
import hashlib
import mmap
from pathlib import Path
from pathlib import PureWindowsPath
import shutil
import struct
import subprocess


def apply_manifest(files, xdk=None):
    """The installer copies shared preview headers/CRTs into the main SDK."""
    manifest = files / 'manifest.csv'
    if not manifest.is_file(): raise RuntimeError('Missing SDK installer manifest')
    count = 0
    def locate(value):
        parsed = PureWindowsPath(value)
        parts = parsed.parts
        if parsed.root or parsed.drive or not parts or any(p in {'.', '..'} or ':' in p for p in parts):
            raise ValueError('Unsafe SDK manifest path')
        return (xdk / Path(*parts[1:])) if xdk is not None and parts[0] == 'XDK' else files / Path(*parts)
    # The legacy manifest contains ANSI bytes, including non-UTF8 comments.
    with manifest.open(newline='', encoding='latin-1') as stream:
        for row in csv.reader(stream):
            if len(row) < 6 or row[2] != 'copy' or not row[5].startswith('XDK\\'): continue
            source, target = locate(row[4]), locate(row[5])
            if not source.is_file(): raise RuntimeError('Missing manifest source: ' + row[4])
            target.parent.mkdir(parents=True, exist_ok=True)
            shutil.copy2(source, target)
            count += 1
    return count


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('installer', type=Path)
    p.add_argument('destination', type=Path)
    p.add_argument('--sha256', required=True)
    a = p.parse_args()
    sha = hashlib.sha256()
    with a.installer.open('rb') as f:
        for data in iter(lambda: f.read(1024 * 1024), b''): sha.update(data)
    if sha.hexdigest() != a.sha256.lower(): raise SystemExit('Installer SHA-256 mismatch')
    cabinets = a.destination / 'cabinets'
    cabinets.mkdir(parents=True, exist_ok=True)
    count = 0
    with a.installer.open('rb') as f, mmap.mmap(f.fileno(), 0, access=mmap.ACCESS_READ) as data:
        at = 0
        while True:
            at = data.find(b'MSCF\x00\x00\x00\x00', at)
            if at < 0: break
            size = struct.unpack_from('<I', data, at + 8)[0]
            if size < 36 or size > len(data) - at:
                at += 8
                continue
            count += 1
            cabinet = cabinets / ('%02d.cab' % count)
            cabinet.write_bytes(data[at:at + size])
            subprocess.run(['cabextract', '-q', '-d', str(a.destination / 'files'), str(cabinet)], check=True)
            at += size
    if not count: raise SystemExit('No embedded CABs found; inspect installer format')
    copied = apply_manifest(a.destination / 'files')
    print('Extracted %d CABs; SHA-256 %s' % (count, sha.hexdigest()))
    print('Applied %d XDK installer copy operations' % copied)


if __name__ == '__main__': main()
