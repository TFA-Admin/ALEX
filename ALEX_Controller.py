# ALEX_Controller.py — launcher.
#
# 2026-09-21: the Controller lives in controller/ now, one module per view
# (see controller/__init__.py for the layout and the reasons). This file
# only starts it, so every shortcut and habit that runs
# `python ALEX_Controller.py` keeps working.
#
# The import order that matters (the sklearn preload before PySide6) is
# handled inside the package's __init__, so it holds for this launcher and
# for anything else that imports controller.app.
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from controller.app import main  # noqa: E402

if __name__ == "__main__":
    main()
