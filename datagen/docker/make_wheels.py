#!/usr/bin/env python3
"""Build stub wheels for every PIP_PKGS entry into /opt/wheels (offline pip index)."""
import os, sys, zipfile
sys.path.insert(0, "/opt/terllm")
from fixtures import CONSOLE_SCRIPTS, PIP_PKGS
os.makedirs("/opt/wheels", exist_ok=True)
for dist, mod in PIP_PKGS.items():
    norm = dist.replace("-", "_")
    di = f"{norm}-1.0.0.dist-info"
    meta = f"Metadata-Version: 2.1\nName: {dist}\nVersion: 1.0.0\n"
    wheel = "Wheel-Version: 1.0\nGenerator: stub\nRoot-Is-Purelib: true\nTag: py3-none-any\n"
    ep = f"[console_scripts]\n{dist} = {mod}:main\n" if dist in CONSOLE_SCRIPTS else ""
    with zipfile.ZipFile(f"/opt/wheels/{norm}-1.0.0-py3-none-any.whl", "w") as z:
        z.writestr(f"{mod}/__init__.py", "def main():\n    print('ok')\n")
        z.writestr(f"{mod}/__main__.py", f"from {mod} import main\nmain()\n")
        z.writestr(f"{di}/METADATA", meta); z.writestr(f"{di}/WHEEL", wheel)
        z.writestr(f"{di}/RECORD", "")
        if ep: z.writestr(f"{di}/entry_points.txt", ep)
