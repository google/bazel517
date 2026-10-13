"""Wheel and PEP 660 editable wheel builders."""

from __future__ import annotations

from collections.abc import Sequence
import pathlib
import shutil
import zipfile

from . import _bazel, _config, _metadata


def _render_editable_module(pth_dirs: Sequence[pathlib.Path]) -> str:
    """Render the runtime `_editable_<pkg>.py` finder module for PEP 660 installs.

    When workspace `.py` files (including `__init__.py`) live in a source root
    such as `src/<pkg>/__init__.py` while compiled C++/pybind extensions and
    generated modules remain in `site-packages/<pkg>/`, Python imports `<pkg>`
    as a regular package with `<pkg>.__path__ = ['/path/to/src/<pkg>']`.
    This `MetaPathFinder` extends `submodule_search_locations` (`__path__`) for
    packages found under `_SOURCE_ROOTS` to also include the corresponding
    `site-packages/<pkg>` directory so compiled extensions and generated files
    inside `<pkg>` are importable alongside live workspace sources.
    """
    roots_literal = ", ".join(repr(str(p)) for p in pth_dirs)
    return f"""\
from __future__ import annotations

from collections.abc import Sequence
import importlib.abc
import importlib.machinery
import pathlib
import sys
import types

_SITE_PACKAGES = pathlib.Path(__file__).resolve().parent
_SOURCE_ROOTS = tuple(pathlib.Path(p) for p in ({roots_literal},))


class _EditableFinder(importlib.abc.MetaPathFinder):
    def find_spec(
        self,
        fullname: str,
        path: Sequence[str] | None,
        target: types.ModuleType | None = None,
    ) -> importlib.machinery.ModuleSpec | None:
        parts = fullname.split(".")
        source_dirs = [
            str(d) for root in _SOURCE_ROOTS if (d := root.joinpath(*parts)).is_dir()
        ]
        if not source_dirs:
            return None
        spec = importlib.machinery.PathFinder.find_spec(fullname, path, target)
        if spec is None or spec.submodule_search_locations is None:
            return None
        locations = list(spec.submodule_search_locations)
        for src_dir in source_dirs:
            if src_dir not in locations:
                locations.insert(0, src_dir)
        installed_dir = _SITE_PACKAGES.joinpath(*parts)
        if installed_dir.is_dir():
            installed_str = str(installed_dir)
            if installed_str not in locations:
                locations.append(installed_str)
        spec.submodule_search_locations = locations
        return spec


_FINDER = _EditableFinder()


def install() -> None:
    if _FINDER not in sys.meta_path:
        sys.meta_path.insert(0, _FINDER)
"""


def _find_record_arcname(zf: zipfile.ZipFile, wheel_name: str) -> str:
    for arcname in zf.namelist():
        if arcname.endswith(".dist-info/RECORD"):
            return arcname
    raise _bazel.BazelError(
        f"Wheel {wheel_name!r} is missing a .dist-info/RECORD entry."
    )


def build_standard_wheel(
    cfg: _config.WheelBuildConfig,
    wheel_dir: pathlib.Path,
) -> str:
    """Build the Bazel wheel target and copy the resulting `.whl` into `wheel_dir`."""
    build_res = _bazel.build_bazel_wheel_target(
        cfg.project_root,
        cfg.bazel,
        editable=False,
    )
    wheel_dir.mkdir(parents=True, exist_ok=True)
    dest_path = wheel_dir / build_res.wheel_path.name
    shutil.copy2(build_res.wheel_path, dest_path)
    return dest_path.name


def build_editable_wheel(
    cfg: _config.WheelBuildConfig,
    wheel_dir: pathlib.Path,
) -> str:
    """Build a PEP 660 editable wheel using source paths queried directly from Bazel."""
    build_res = _bazel.build_bazel_wheel_target(
        cfg.project_root,
        cfg.bazel,
        editable=True,
    )
    layout = _bazel.query_wheel_source_layout(
        cfg.project_root,
        cfg.bazel,
        editable=True,
        expanded_args=build_res.expanded_args,
    )

    wheel_dir.mkdir(parents=True, exist_ok=True)
    dest_path = wheel_dir / build_res.wheel_path.name

    record_entries: list[tuple[str, str, int | str]] = []

    with (
        zipfile.ZipFile(build_res.wheel_path, mode="r") as src_zf,
        zipfile.ZipFile(
            dest_path, mode="w", compression=zipfile.ZIP_DEFLATED
        ) as dst_zf,
    ):
        record_arcname = _find_record_arcname(src_zf, build_res.wheel_path.name)
        dist_info_prefix = record_arcname.removesuffix(".dist-info/RECORD")
        dist_name = dist_info_prefix.split("-", maxsplit=1)[0]
        norm_name = _config.distribution_filename_component(dist_name)

        module_stem = f"_editable_{norm_name}"
        finder_arcname = f"{module_stem}.py"
        finder_bytes = _render_editable_module(layout.pth_dirs).encode("utf-8")

        pth_arcname = f"{module_stem}.pth"
        pth_lines = [
            *(str(p) for p in layout.pth_dirs),
            f"import {module_stem}; {module_stem}.install()",
        ]
        pth_bytes = ("\n".join(pth_lines) + "\n").encode("utf-8")

        for info in src_zf.infolist():
            arcname = info.filename
            if arcname.endswith("/") or arcname == record_arcname:
                continue
            if arcname in layout.source_arcnames and pathlib.PurePosixPath(
                arcname
            ).suffix in {".py", ".pyi"}:
                continue

            data = src_zf.read(arcname)
            dst_zf.writestr(info, data)
            digest, size = _metadata.record_hash(data)
            record_entries.append((arcname, digest, size))

        for extra_arcname, extra_bytes in (
            (finder_arcname, finder_bytes),
            (pth_arcname, pth_bytes),
        ):
            dst_zf.writestr(extra_arcname, extra_bytes)
            digest, size = _metadata.record_hash(extra_bytes)
            record_entries.append((extra_arcname, digest, size))

        record_bytes = _metadata.format_record(record_entries, record_arcname).encode(
            "utf-8"
        )
        dst_zf.writestr(record_arcname, record_bytes)

    return dest_path.name
