"""The installed agenthicc version, readable without importing the CLI parser."""

from __future__ import annotations


def package_version() -> str:
    """Return the installed distribution's version, which is the wheel's version.

    An uninstalled source tree (``PYTHONPATH=src``) has no distribution
    metadata, so it reports ``0+unknown`` instead of a version it cannot know.
    """
    from importlib.metadata import PackageNotFoundError, version

    try:
        return version("agenthicc")
    except PackageNotFoundError:
        return "0+unknown"
