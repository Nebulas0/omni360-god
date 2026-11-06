"""Build and run portable client contract tests; does not claim an XEX build."""
import os
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
