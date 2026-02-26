#!/usr/bin/env python3
"""Install MeshCoreInterface into the Reticulum interfaces directory."""

import shutil
import sys
from pathlib import Path

CONFIGDIR = Path(sys.argv[1]) if len(sys.argv) > 1 else Path.home() / ".reticulum"
INTERFACES_DIR = CONFIGDIR / "interfaces"
HERE = Path(__file__).parent

# Check dependencies
try:
    import meshcore  # noqa: F401
except ImportError:
    print("Error: 'meshcore' Python package is not installed.")
    print("Install it with:  pip install rns meshcore pyserial bleak")
    sys.exit(1)

# Create interfaces directory and copy files
INTERFACES_DIR.mkdir(parents=True, exist_ok=True)

shutil.copy2(HERE / "MeshCoreInterface.py", INTERFACES_DIR / "MeshCoreInterface.py")

dest_pkg = INTERFACES_DIR / "meshcore_interface"
if dest_pkg.exists():
    shutil.rmtree(dest_pkg)
shutil.copytree(HERE / "meshcore_interface", dest_pkg)

print(f"Installed to {INTERFACES_DIR}/")
print()
print("Next steps:")
print(f"  1. Add a [[MeshCore LoRa]] section to {CONFIGDIR / 'config'}")
print("     (see config_example.ini or README.md for examples)")
print("  2. Run: rnsd -v")
