"""Compile the actual Xbox Release projects with privately supplied XDK tools.

Runs under Wine on Linux. Signing is a separate host-side step.
"""
import argparse
import atexit
from concurrent.futures import ThreadPoolExecutor
import fcntl
import hashlib
import json
import os
from pathlib import Path
import re
import subprocess
import struct
import time
import xml.etree.ElementTree as ET

NS = {'m': 'http://schemas.microsoft.com/developer/msbuild/2003'}
CONFIG = "'$(Configuration)|$(Platform)'=='Release|Xbox 360'"
SDK, SOURCE, BUILD = Path('/xdk'), Path('/source'), Path('/build')
HEADER_HASH = ''


def inspect_xex(data):
    """Validate the actual SDK title format before signing or offering a build."""
    def require(condition, message):
        if not condition: raise ValueError('Invalid XEX: ' + message)
    require(len(data) >= 24, 'truncated header')
    magic, flags, payload, _, security, count = struct.unpack_from('>6I', data)
    require(magic == 0x58455832 and flags == 1, 'not an Xbox title module')
    require(0 < count <= 128 and 24 + count * 8 <= payload < len(data), 'optional-header bounds')
    require(security >= 24 + count * 8 and security + 8 <= payload, 'security-header bounds')
    headers = dict(struct.unpack_from('>II', data, 24 + i * 8) for i in range(count))
    require(len(headers) == count, 'duplicate optional header')
    require({0x2FF, 0x3FF, 0x10100, 0x10201, 0x20200, 0x40006} <= set(headers), 'missing loader fields')
    base, entry = headers[0x10201], headers[0x10100]
    image_size = struct.unpack_from('>I', data, security + 4)[0]
    require(base == 0x82000000 and 0 < image_size <= 64 * 1024 * 1024, 'image mapping')
    require(base <= entry < base + image_size and entry % 4 == 0, 'entry point outside image')
    execution = headers[0x40006]
    require(execution >= 24 and execution + 24 <= payload, 'execution-ID bounds')
    title_id = struct.unpack_from('>I', data, execution + 12)[0]
    require(title_id == 0xFFFF0360, 'wrong title ID')
    resources = headers[0x2FF]
    require(resources >= 24 and resources + 4 <= payload, 'resource bounds')
    length = struct.unpack_from('>I', data, resources)[0]
    require(length >= 4 and (length - 4) % 16 == 0 and resources + length <= payload, 'resource table length')
    names = []
    for at in range(resources + 4, resources + length, 16):
        name, address, size = struct.unpack_from('>8sII', data, at)
        require(size > 0 and base <= address < base + image_size and size <= base + image_size - address, 'resource outside image')
        names.append(name.rstrip(b'\0').decode('ascii'))
    require({'font', 'selawk', 'ofl'} <= set(names), 'missing embedded fonts or license')
    fmt = headers[0x3FF]
    require(fmt >= 24 and fmt + 8 <= payload, 'file-format bounds')
    length, encryption, compression = struct.unpack_from('>IHH', data, fmt)
    require(length >= 16 and (length - 8) % 8 == 0 and fmt + length <= payload, 'basic block-table length')
    require(encryption == 1 and compression == 1, 'unexpected SDK packaging mode')
    blocks = [struct.unpack_from('>II', data, at) for at in range(fmt + 8, fmt + length, 8)]
    require(sum(n for n, zeros in blocks) == len(data) - payload, 'truncated or surplus payload')
    require(sum(n + zeros for n, zeros in blocks) == image_size, 'mapped image size differs')
    return {'title_id': '%08X' % title_id, 'image_base': hex(base), 'entry_point': hex(entry),
            'image_bytes': image_size, 'stack_bytes': headers[0x20200], 'resources': sorted(names),
            'encryption': encryption, 'compression': compression}


def win(path): return 'Z:' + str(Path(path).resolve()).replace('/', '\\')


def resolve_case(path):
    """VS projects use Windows filename case; the Linux source tree does not."""
    current = Path(path.anchor)
    for part in path.parts[1:]:
        exact = current / part
        if exact.exists(): current = exact; continue
        matches = [p for p in current.iterdir() if p.name.casefold() == part.casefold()]
        if len(matches) != 1: raise ValueError('Missing or ambiguous source: ' + str(path))
        current = matches[0]
    return current


def applicable(element):
    condition = element.get('Condition')
    if not condition or condition == CONFIG: return True
    if '$(Configuration)|$(Platform)' in condition: return False
    raise ValueError('Unsupported project condition: ' + condition)


def properties(element):
    return {e.tag.split('}')[-1]: e.text or '' for e in element if applicable(e)}


def project(path):
    root = ET.parse(path).getroot()
    groups = [e for e in root.findall('m:ItemDefinitionGroup', NS) if e.get('Condition') == CONFIG]
    if len(groups) != 1: raise ValueError('Missing unique Xbox Release configuration')
    defaults = properties(groups[0].find('m:ClCompile', NS))
    link = groups[0].find('m:Link', NS)
    link_options = properties(link) if link is not None else {}
    files = []
    for item in root.findall('m:ItemGroup/m:ClCompile', NS):
        if not applicable(item): continue
        options = dict(defaults)
        for key, value in properties(item).items():
            options[key] = value.replace('%(' + key + ')', defaults.get(key, ''))
        if options.get('ExcludedFromBuild', 'false').lower() == 'true': continue
        source = resolve_case(path.parent / item.attrib['Include'].replace('\\', '/'))
        if not source.is_file(): raise ValueError('Missing source: ' + str(source))
        files.append((source, options))
    if not files: raise ValueError('No sources')
    return files, link_options


def expand_list(value, directory):
    value = value.replace('$(ProjectDir)', str(directory) + '/').replace('\\', '/')
    return [p for p in value.split(';') if p and not p.startswith('%(')]


def check_sdk():
    needed = ['bin/win32/' + f for f in ('cl.exe', 'c1.dll', 'c1xx.dll', 'c2.dll', 'lib.exe', 'link.exe', 'imagexex.exe', 'Bundler.exe')]
    needed += ['include/xbox/xtl.h', 'lib/xbox/xboxkrnl.lib', 'lib/xbox/d3d9.lib', 'lib/xbox/d3dx9.lib', 'lib/xbox/xapilib.lib']
    missing = [p for p in needed if not (SDK / p).is_file()]
    if missing: raise SystemExit('Incomplete XDK: ' + ', '.join(missing))


def invoke(tool, args, name, directory=SOURCE):
    log, response = BUILD / 'logs' / (name + '.log'), BUILD / 'rsp' / (name + '.rsp')
    # No shell. Response files retain literal Windows backslashes.
    response.write_text('\n'.join('"' + arg + '"' for arg in args) + '\n')
    arguments = ['@' + win(response)] if tool.lower() in {'cl.exe', 'link.exe', 'lib.exe'} else args
    with log.open('wb') as output:
        result = subprocess.run(['wine', str(SDK / 'bin/win32' / tool)] + arguments,
                                cwd=directory, stdout=output, stderr=subprocess.STDOUT, timeout=600)
    if result.returncode:
        tail = '\n'.join(log.read_text(errors='replace').splitlines()[-45:])
        raise RuntimeError('%s failed (%d):\n%s' % (tool, result.returncode, tail))


def build_project(name, path, jobs):
    files, link_options = project(path)
    objects = []
    def compile_one(entry):
        source, options = entry
        relative = str(source.relative_to(SOURCE))
        ident = name + '-' + hashlib.sha256(relative.encode()).hexdigest()[:16]
        obj = BUILD / 'obj' / (ident + '.obj')
        flags = ['/nologo', '/c', '/O2', '/Ob2', '/Oi', '/Ot', '/GF', '/Gy', '/MT', '/GS-', '/GR-', '/EHs-c-', '/Zc:wchar_t', '/Zc:forScope', '/fp:fast', '/fp:except-', '/Z7', '/W0', '/D_MBCS', '/FI' + win(SDK / 'include/xbox/xbox_intellisense_platform.h')]
        if options.get('WholeProgramOptimization') == 'true': flags.append('/GL')
        flags += ['/D' + d for d in expand_list(options.get('PreprocessorDefinitions', ''), path.parent)]
        includes = [path.parent, SOURCE / 'src', SOURCE / 'Common', SOURCE / 'SSL']
        includes += [Path(p) if Path(p).is_absolute() else path.parent / p for p in expand_list(options.get('AdditionalIncludeDirectories', ''), path.parent)]
        flags += ['/I' + win(p) for p in includes]
        if options.get('CompileAs') == 'CompileAsC': flags.append('/TC')
        if options.get('CompileAs') == 'CompileAsCpp': flags.append('/TP')
        if (BUILD / 'BuildVersion.h').is_file(): flags += ['/FI' + win(BUILD / 'BuildVersion.h')]
        flags += ['/Fo' + win(obj), win(source)]
        signature = hashlib.sha256((json.dumps(flags) + source.read_text(errors='replace') + HEADER_HASH).encode()).hexdigest()
        stamp = obj.with_suffix('.sha256')
        if not obj.exists() or not stamp.exists() or stamp.read_text() != signature:
            invoke('cl.exe', flags, ident)
            stamp.write_text(signature)
        return obj
    print('Compiling %s: %d source files' % (name, len(files)), flush=True)
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for obj in pool.map(compile_one, files): objects.append(obj)
    if name == 'atg':
        invoke('lib.exe', ['/nologo', '/OUT:' + win(BUILD / 'AtgFramework.lib')] + [win(p) for p in objects], 'atg-library')
    return objects, link_options


def main():
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument('--check-sdk', action='store_true')
    p.add_argument('--jobs', type=int, default=int(os.getenv('OMNI360_BUILD_JOBS', '4')))
    p.add_argument('--version', help='Generated prerelease version, recorded in build.json')
    args = p.parse_args()
    check_sdk()
    if args.check_sdk: return
    lock = (BUILD / 'native.lock').open('w')
    try: fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError: raise SystemExit('Another native build is still running')
    for sub in ('home', 'logs', 'rsp', 'obj', 'Release', 'Release/Media/Fonts'):
        (BUILD / sub).mkdir(parents=True, exist_ok=True)
    version = re.search(r'^#define CURRENT_VERSION "([^"]+)"', (SOURCE / 'src/settings.h').read_text(), re.M).group(1)
    base_version = version
    if args.version:
        if '#ifndef CURRENT_VERSION' not in (SOURCE / 'src/settings.h').read_text():
            raise SystemExit('Source does not support a generated build version')
        if not re.fullmatch(re.escape(version) + r'\.build\.[1-9][0-9]*', args.version) or '-' not in version:
            raise SystemExit('Generated version must extend the source prerelease with .build.N')
        version = args.version
    (BUILD / 'BuildVersion.h').write_text('#define CURRENT_VERSION "' + version + '"\n')
    os.environ['INCLUDE'] = ';'.join(win(SDK / d) for d in ('include/xbox', 'include/xbox/sys'))
    os.environ['LIB'] = win(SDK / 'lib/xbox')
    # The XDK PDB server can outlive a finished linker. Waiting for every Wine
    # process on a warm prefix would hang the next build. This prefix is used
    # only by the locked compiler job; stop its previous services first.
    subprocess.run(['wineserver', '-k'], check=False)
    subprocess.run(['wineserver', '-w'], check=True, timeout=30)
    atexit.register(lambda: subprocess.run(['wineserver', '-k'], check=False, timeout=30))
    if not (Path(os.environ['WINEPREFIX']) / 'system.reg').is_file():
        subprocess.run(['wineboot', '-u'], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, check=True, timeout=120)
    global HEADER_HASH
    digest = hashlib.sha256()
    for path in sorted(SOURCE.rglob('*')):
        if path.suffix.lower() in {'.h', '.hpp', '.inl', '.inc'} and '.git' not in path.parts:
            digest.update(str(path.relative_to(SOURCE)).encode()); digest.update(path.read_bytes())
    HEADER_HASH = digest.hexdigest()
    HEADER_HASH += version
    marker = SDK / 'toolchain.json'
    if marker.is_file(): HEADER_HASH += hashlib.sha256(marker.read_bytes()).hexdigest()
    for name in ('cl.exe', 'c1.dll', 'c1xx.dll', 'c2.dll'):
        HEADER_HASH += hashlib.sha256((SDK / 'bin/win32' / name).read_bytes()).hexdigest()
    build_project('atg', SOURCE / 'Common/AtgFramework2010.vcxproj', args.jobs)
    objects, link = build_project('omni360', SOURCE / 'XboxTLS2.vcxproj', args.jobs)
    release = BUILD / 'Release'
    executable, xex = release / 'Omni360.exe', release / 'Omni360.xex'
    xex.unlink(missing_ok=True)
    libraries = [win(BUILD / lib) if lib == 'AtgFramework.lib' else lib for lib in expand_list(link['AdditionalDependencies'], SOURCE)]
    invoke('link.exe', ['/nologo', '/INCREMENTAL:NO', '/SUBSYSTEM:XBOX', '/XEX:NO', '/FIXED:NO', '/LTCG', '/OPT:REF', '/OPT:ICF', '/RELEASE', '/STACK:262144,262144', '/OUT:' + win(executable)] + [win(obj) for obj in objects] + libraries, 'link')
    font = release / 'Media/Fonts/font.xpr'
    invoke('Bundler.exe', [win(SOURCE / 'Media/Fonts/uifont_16.rdf'), '-o', win(font)], 'font', SOURCE / 'Media/Fonts')
    invoke('imagexex.exe', ['/nologo', '/IN:' + win(executable), '/OUT:' + win(xex), '/TITLEID:0xFFFF0360', '/CONFIG:' + win(SOURCE / 'xex.xml'), '/SECTION:font=' + win(font), '/SECTION:selawk=' + win(SOURCE / 'Media/Fonts/selawk.ttf'), '/SECTION:ofl=' + win(SOURCE / 'Media/Fonts/OFL.txt')], 'imagexex')
    if not xex.is_file() or xex.read_bytes()[:4] != b'XEX2': raise SystemExit('No valid XEX2 output')
    report = {'version': version, 'source_base_version': base_version, 'sha256': hashlib.sha256(xex.read_bytes()).hexdigest(), 'bytes': xex.stat().st_size,
              'built_at': time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime()), 'sdk_cl_sha256': hashlib.sha256((SDK / 'bin/win32/cl.exe').read_bytes()).hexdigest()}
    report['xex_loader'] = inspect_xex(xex.read_bytes())
    if marker.is_file():
        imported = json.loads(marker.read_text())
        report['sdk_version'] = imported['sdk_version']
        report['sdk_installer_sha256'] = imported['sdk_installer_sha256']
    (release / 'build.json').write_text(json.dumps(report, indent=2) + '\n')
    print(json.dumps(report), flush=True)


if __name__ == '__main__': main()
