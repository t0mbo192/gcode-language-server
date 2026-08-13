"""context.py — makes `server/` importable from the tests.

The server modules import each other by bare name (`from dialects import ...`)
because they run as a standalone program with `server/` as the working
directory — that is what keeps gcode_parser.py dependency-free and runnable
as `python server/gcode_parser.py file.nc`. Tests live outside that
directory, so they need the path fixed up exactly once, here.

Every test module starts with `from context import ...` instead of repeating
four lines of sys.path surgery.
"""

import os
import sys

REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SERVER_DIR = os.path.join(REPO_ROOT, "server")
EXAMPLES_DIR = os.path.join(REPO_ROOT, "examples")

if SERVER_DIR not in sys.path:
    sys.path.insert(0, SERVER_DIR)

import dialects            # noqa: E402  (path must be set first)
import gcode_parser        # noqa: E402

__all__ = ["REPO_ROOT", "SERVER_DIR", "EXAMPLES_DIR", "dialects",
           "gcode_parser"]
