import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from harness_react import main  # noqa: E402
from config import CONFIG  # noqa: E402

if __name__ == "__main__":
    main(CONFIG)
