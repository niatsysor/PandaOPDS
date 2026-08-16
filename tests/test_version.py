"""Version consistency: pyproject.toml must match app.__version__.

The app version has a single source of truth in ``app/__init__.py``; the
packaging metadata (pyproject.toml) mirrors it. This test fails loudly when
they drift so a release never ships a WebUI version that disagrees with the
installed package.
"""

import tomllib
from pathlib import Path

from app import __version__


def test_pyproject_version_matches_app():
    pyproject = tomllib.loads(
        (Path(__file__).parent.parent / "pyproject.toml").read_text(encoding="utf-8")
    )
    assert pyproject["project"]["version"] == __version__, (
        "pyproject.toml version != app.__version__; bump both together"
    )
