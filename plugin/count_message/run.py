"""Run the Python backend and JavaScript UI together from an extracted ZIP."""
from pathlib import Path
import sys

root = Path(__file__).resolve().parent
sys.path.insert(0, str(root / "backend"))
from count_message.server import main

if __name__ == "__main__":
    sys.argv[1:1] = ["--ui-dir", str(root / "ui")]
    main()
