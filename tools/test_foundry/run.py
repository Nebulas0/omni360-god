"""Build and run portable client contract tests; does not claim an XEX build."""
import os
import platform
import shutil
from pathlib import Path
import subprocess
import tempfile

root = Path(__file__).resolve().parents[2]
fixtures = root / "tools/test_foundry/fixtures"
if not (fixtures / "prepared.json").is_file():
    raise SystemExit("Generate the synthetic API contract fixtures first; see docs/FOUNDRY-DEVELOPMENT.md")
with tempfile.TemporaryDirectory(prefix="foundry-protocol-") as temp:
    obj, binary = Path(temp) / "cJSON.o", Path(temp) / "test_protocol"
    subprocess.run([os.getenv("CC", "cc"), "-std=c99", "-c", str(root / "src/cJSON.c"), "-o", str(obj)], check=True)
    subprocess.run([os.getenv("CXX", "c++"), "-std=c++03", "-Wall", "-Wextra", "-I", str(root / "src"),
                    str(root / "src/FoundryProtocol.cpp"), str(root / "tools/test_foundry/test_protocol.cpp"),
                    str(obj), "-o", str(binary)], check=True)
    subprocess.run([str(binary), str(fixtures)], check=True)
    clang, clangxx = shutil.which("clang"), shutil.which("clang++")
    if not clang or not clangxx:
        raise SystemExit("clang and clang++ are required for the actual client runtime tests")
    objects = [str(obj)]
    for source in ("hash/sha1.c", "hash/sha2small.c", "codec/enc32be.c", "codec/dec32be.c"):
        target = Path(temp) / (Path(source).stem + ".o")
        subprocess.run([clang, "-std=c99", "-fms-extensions", "-I", str(root / "SSL"),
                        "-c", str(root / "SSL" / source), "-o", str(target)], check=True)
        objects.append(str(target))
    runtime = Path(temp) / "test_runtime"
    linker_gc = "-Wl,-dead_strip" if platform.system() == "Darwin" else "-Wl,--gc-sections"
    subprocess.run([clangxx, "-std=c++03", "-fms-extensions", "-ffunction-sections", "-fdata-sections",
                    "-D_XBOX", "-I", str(root / "tools/test_foundry/shim"), "-I", str(root / "src"),
                    "-I", str(root / "SSL"), "-Wno-deprecated-declarations", "-Wno-format",
                    str(root / "tools/test_foundry/test_runtime.cpp"), str(root / "src/FoundryProtocol.cpp"),
                    *objects, linker_gc, "-o", str(runtime)], check=True)
    test_root = Path(temp) / "filesystem"
    test_root.mkdir()
    subprocess.run([str(runtime)], env={**os.environ, "FOUNDRY_TEST_ROOT": str(test_root)}, check=True)
