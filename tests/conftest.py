import os
import sys

_here = os.path.dirname(os.path.abspath(__file__))
# App root (for `import server`) and this directory (for `import fake_platform`).
# The repository root has its own `tests` package, so fixtures are imported by
# module name rather than as `tests.fake_platform`.
sys.path.insert(0, os.path.dirname(_here))
sys.path.insert(0, _here)
