#!/usr/bin/env python3
"""Download, retain three versions and publish the bitwarden repository."""
from pathlib import Path
import sys

# Resolve symlinks so this also runs through /usr/local/sbin/ from any directory.
sys.path.insert(0, str(Path(__file__).resolve().parents[2]))
from common.cli import main

if __name__ == "__main__":
    raise SystemExit(main([*sys.argv[1:], "--repo", "bitwarden"]))
