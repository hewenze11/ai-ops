"""Ensure the package root is importable when running tests from any cwd."""
import sys
from pathlib import Path

root = str(Path(__file__).resolve().parent)
if root not in sys.path:
    sys.path.insert(0, root)
