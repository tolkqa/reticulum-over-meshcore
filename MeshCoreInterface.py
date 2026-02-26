"""
Reticulum custom interface loader for MeshCoreInterface.

Place this file and the meshcore_interface/ package into:
    ~/.reticulum/interfaces/

The easiest way is to run ./install.sh from the project directory.
"""

import sys
import os

# Add the interfaces directory to sys.path so meshcore_interface/ package
# sitting next to this file is importable without pip install or PYTHONPATH.
#
# RNS.Reticulum.interfacepath is set in __init__ before interfaces are loaded,
# so it is available at exec() time. But as a fallback we also check the
# default location.
_iface_dir = None
try:
    import RNS
    _p = RNS.Reticulum.interfacepath
    if _p:
        _iface_dir = os.path.abspath(_p)
except Exception:
    pass

if not _iface_dir:
    _iface_dir = os.path.expanduser("~/.reticulum/interfaces")

if _iface_dir not in sys.path:
    sys.path.insert(0, _iface_dir)

from meshcore_interface.interface import MeshCoreInterface

interface_class = MeshCoreInterface
