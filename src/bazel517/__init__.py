"""PEP 517 / PEP 660 Python build backend wrapping Bazel."""

from __future__ import annotations

from collections.abc import Mapping
import pathlib
from typing import Any, Final

from . import _config, _sdist, _wheel
from ._bazel import BazelError

__version__: Final[str] = "0.1.0"

__all__ = (
    "BazelError",
    "__version__",
    "build_editable",
    "build_sdist",
    "build_wheel",
    "get_requires_for_build_editable",
    "get_requires_for_build_sdist",
    "get_requires_for_build_wheel",
)


def get_requires_for_build_wheel(
    config_settings: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return additional build requirements for building a wheel (PEP 517)."""
    del config_settings
    return []


def get_requires_for_build_sdist(
    config_settings: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return additional build requirements for building an sdist (PEP 517)."""
    del config_settings
    return []


def get_requires_for_build_editable(
    config_settings: Mapping[str, Any] | None = None,
) -> list[str]:
    """Return additional build requirements for building an editable wheel (PEP 660)."""
    del config_settings
    return []


def build_wheel(
    wheel_directory: str,
    config_settings: Mapping[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    """Build a standard wheel in `wheel_directory` and return its basename."""
    del metadata_directory
    cfg = _config.load_wheel_config(config_settings=config_settings)
    return _wheel.build_standard_wheel(cfg, pathlib.Path(wheel_directory))


def build_editable(
    wheel_directory: str,
    config_settings: Mapping[str, Any] | None = None,
    metadata_directory: str | None = None,
) -> str:
    """Build an editable wheel in `wheel_directory` and return its basename."""
    del metadata_directory
    cfg = _config.load_wheel_config(config_settings=config_settings)
    return _wheel.build_editable_wheel(cfg, pathlib.Path(wheel_directory))


def build_sdist(
    sdist_directory: str,
    config_settings: Mapping[str, Any] | None = None,
) -> str:
    """Build a source distribution in `sdist_directory` and return its basename."""
    del config_settings
    cfg = _config.load_sdist_config()
    return _sdist.build_source_distribution(cfg, pathlib.Path(sdist_directory))
