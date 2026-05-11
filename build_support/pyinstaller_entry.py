"""PyInstaller entry point.

PyInstaller runs this file as a plain script, so keep package-relative imports
inside the real package and call the public entry point here.
"""

from hash256_miner import main


if __name__ == "__main__":
    raise SystemExit(main())
