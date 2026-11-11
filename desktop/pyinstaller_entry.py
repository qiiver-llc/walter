"""PyInstaller entry point (desktop/walter-desktop.spec): the same CLI as python -m desktop."""
import sys

from desktop.launcher import main

if __name__ == "__main__":
    sys.exit(main())
