import hashlib
import struct
from pathlib import Path


def synthetic_iso(title_id=0x415607E1, media_id=0x12345678, base_version=2, disc_number=1, disc_count=1):
    image = bytearray(0x12000)
    image[0x10000:0x10014] = b"MICROSOFT*XBOX*MEDIA"
    struct.pack_into("<II", image, 0x10014, 0x21, 0x800)
    name = b"default.xex"
    struct.pack_into("<HHIIBB", image, 0x10800, 0, 0, 0x22, 0x200, 0x20, len(name))
    image[0x1080E:0x1080E + len(name)] = name
    struct.pack_into("<HH", image, 0x1081C, 0xFFFF, 0xFFFF)
    image[0x11000:0x11004] = b"XEX2"
    struct.pack_into(">III", image, 0x11014, 1, 0x40006, 0x100)
    struct.pack_into(">IIIIBBBB", image, 0x11100, media_id, base_version, base_version, title_id, 0, 0, disc_number, disc_count)
    return bytes(image)


def write_god(title_root: Path, *, base_version=2, title_id=0x415607E1, media_id=0x12345678, disc_number=1, disc_count=1, basename=None):
    image = synthetic_iso(title_id, media_id, base_version, disc_number, disc_count)
    table = bytearray(4096)
    for i in range(len(image) // 4096):
        table[i*20:(i+1)*20] = hashlib.sha1(image[i*4096:(i+1)*4096]).digest()
    master = bytearray(4096)
    master[:20] = hashlib.sha1(table).digest()
    package = title_root / "00007000"
    basename = basename or f"{media_id:08X}"
    data = package / (basename + ".data")
    data.mkdir(parents=True)
    fragment = data / "Data0000"
    fragment.write_bytes(master + table + image)
    header = bytearray(0xB000)
    header[:4] = b"LIVE"
    header[0x22C:0x234] = b"\xff"*8
    header[0x340:0x344] = (0xAD0E).to_bytes(4,"big")
    header[0x344:0x348] = (0x7000).to_bytes(4,"big")
    header[0x354:0x358] = media_id.to_bytes(4,"big")
    header[0x360:0x364] = title_id.to_bytes(4,"big")
    header[0x366:0x368] = bytes((disc_number, disc_count))
    header[0x37D:0x391] = hashlib.sha1(master).digest()
    header[0x392:0x395] = (len(image)//4096).to_bytes(3,"big")
    header[0x3A0:0x3A4] = (1).to_bytes(4,"little")
    header[0x3A4:0x3A8] = (fragment.stat().st_size//256).to_bytes(4,"big")
    header[0x32C:0x340] = hashlib.sha1(header[0x344:]).digest()
    path = package / basename
    path.write_bytes(header)
    return path


def write_tu(path: Path, *, title_id=0x415607E1, media_id=0x12345678, base_version=2, version=1):
    header = bytearray(0xB000)
    header[:4] = b"LIVE"
    header[0x22C:0x234] = b"\xff"*8
    struct.pack_into(">II", header, 0x340, 0xAD0E, 0xB0000)
    struct.pack_into(">IIII", header, 0x354, media_id, version, base_version, title_id)
    header[0x32C:0x340] = hashlib.sha1(header[0x344:]).digest()
    payload = bytes(header) + b"T"*4096
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(payload)
    return payload
