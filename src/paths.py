"""Project-root resolution so scripts keep working wherever the project lives.

Every script that imports the src package must add the project root to
sys.path first. Importing this module does that: the root is derived from
this file's location, never hardcoded.
"""

import sys
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

if str(PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(PROJECT_ROOT))

DATA_DIR = PROJECT_ROOT / "data"
OUTPUT_DIR = PROJECT_ROOT / "output"