import os
import sys

# Put the repo root on sys.path so `import config` / `import db` resolve when
# pytest is run from anywhere. Tests live in testing/ but the modules they test
# live at the repo root.
REPO_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if REPO_ROOT not in sys.path:
    sys.path.insert(0, REPO_ROOT)
