"""PEP 517 / PEP 660 Python build backend wrapping Bazel."""

from __future__ import annotations

from typing import Final

from . import _backend, _bazel

__version__: Final[str] = "0.1.0"

BazelError = _bazel.BazelError
build_editable = _backend.build_editable
build_sdist = _backend.build_sdist
build_wheel = _backend.build_wheel
get_requires_for_build_editable = _backend.get_requires_for_build_editable
get_requires_for_build_sdist = _backend.get_requires_for_build_sdist
get_requires_for_build_wheel = _backend.get_requires_for_build_wheel

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
