"""Repo-root conftest — adds repo root to sys.path so tests can import
``skills.babysit.assets.db`` and other modules under ``skills/`` as packages.

The empty ``__init__.py`` files under ``skills/`` exist purely to make this
import path work in tests; they are not needed at skill runtime.
"""

from __future__ import annotations

import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
