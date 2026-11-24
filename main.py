"""Launcher for the necromod GUI.

Run with ``uv run main.py [package.necromod]`` or ``uv run necromod-gui``.
"""

from necromod.gui import main

if __name__ == "__main__":
    raise SystemExit(main())
