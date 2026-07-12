import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent / "app"))

from service import get_status


def main():
    status = get_status()
    assert status["ok"] is True
    print("healthcheck ok")


if __name__ == "__main__":
    main()
