"""Standalone launcher: starts the transcript_bot Flask server.

This avoids the `python -m` module entry which sometimes has issues under
Hermes background terminals. It calls main() directly.
"""
import sys
from pathlib import Path

# Ensure project root is importable
ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from transcript_bot.web import main

if __name__ == "__main__":
    main()
