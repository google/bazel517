# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Configuration parsing for pyproject.toml ([project] and [tool.bazel517])."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import dataclasses
import os
import pathlib
import re
import shlex
import sys
import sysconfig
import tomllib
import types
from typing import Any, Final

import packaging.version

_CANONICALIZE_RE: Final[re.Pattern[str]] = re.compile(r"[-_.]+")
_EMPTY_STR_MAP: Final[Mapping[str, str]] = types.MappingProxyType({})
_EMPTY_OPT_DEPS: Final[Mapping[str, tuple[str, ...]]] = types.MappingProxyType({})
_README_CONTENT_TYPES: Final[Mapping[str, str]] = types.MappingProxyType(
    {
        ".md": "text/markdown",
        ".rst": "text/x-rst",
        ".txt": "text/plain",
    }
)


def distribution_filename_component(name: str) -> str:
    """Normalize a distribution name component for wheel and sdist filenames."""
    return _CANONICALIZE_RE.sub("_", name).lower()


def version_filename_component(version: str) -> str:
    """Normalize a version component according to PEP 440 / PEP 625."""
    return str(packaging.version.Version(version.strip()))


@dataclasses.dataclass(frozen=True, slots=True)
class Readme:
    """Parsed README text and MIME content-type."""

    text: str
    content_type: str


@dataclasses.dataclass(frozen=True, slots=True)
class BazelConfig:
    """Configuration from [tool.bazel517] merged with PEP 517 config_settings."""

    target: str
    bazel_bin: str | None = None
    bazel_args: tuple[str, ...] = ()
    editable_bazel_args: tuple[str, ...] = ()
    env: Mapping[str, str] = dataclasses.field(default_factory=lambda: _EMPTY_STR_MAP)


@dataclasses.dataclass(frozen=True, slots=True)
class ProjectMetadata:
    """Parsed PEP 621 [project] metadata."""

    name: str
    version: str
    description: str = ""
    readme: Readme | None = None
    requires_python: str | None = None
    license_expression: str | None = None
    license_text: str | None = None
    license_files: tuple[str, ...] = ()
    authors: tuple[Mapping[str, str], ...] = ()
    maintainers: tuple[Mapping[str, str], ...] = ()
    keywords: tuple[str, ...] = ()
    classifiers: tuple[str, ...] = ()
    urls: Mapping[str, str] = dataclasses.field(default_factory=lambda: _EMPTY_STR_MAP)
    dependencies: tuple[str, ...] = ()
    optional_dependencies: Mapping[str, tuple[str, ...]] = dataclasses.field(
        default_factory=lambda: _EMPTY_OPT_DEPS
    )


@dataclasses.dataclass(frozen=True, slots=True)
class WheelBuildConfig:
    """Configuration required to build standard and editable wheels via Bazel."""

    project_root: pathlib.Path
    bazel: BazelConfig


@dataclasses.dataclass(frozen=True, slots=True)
class SdistBuildConfig:
    """Configuration required to build a PEP 625 source distribution."""

    project_root: pathlib.Path
    metadata: ProjectMetadata


def python_template_vars() -> Mapping[str, str]:
    """Return template variables describing the active Python interpreter."""
    major, minor = sys.version_info.major, sys.version_info.minor
    py_ver = f"{major}.{minor}"
    py_nodot = f"{major}{minor}"
    is_freethreaded = bool(sysconfig.get_config_var("Py_GIL_DISABLED"))
    abi_suffix = "t" if is_freethreaded else ""
    return types.MappingProxyType(
        {
            "python_version": py_ver,
            "python_version_nodot": py_nodot,
            "python_version_abi": f"{py_ver}{abi_suffix}",
            "python_abi_tag": f"cp{py_nodot}{abi_suffix}",
            "freethreaded": "yes" if is_freethreaded else "no",
            "python_executable": sys.executable,
        }
    )


def interpolate_arg(arg: str, variables: Mapping[str, str]) -> str:
    """Interpolate `{python_version}`-style placeholders in a Bazel argument."""
    return arg.format_map(variables)


def _extract_config_setting_list(
    config_settings: Mapping[str, Any] | None,
    *keys: str,
) -> Sequence[str]:
    if not config_settings:
        return ()
    result: list[str] = []
    for key in keys:
        if key not in config_settings:
            continue
        raw = config_settings[key]
        if isinstance(raw, str):
            result.extend(shlex.split(raw))
        else:
            for item in raw:
                result.extend(shlex.split(str(item)))
    return tuple(result)


def _infer_readme_content_type(readme_path: pathlib.Path) -> str:
    return _README_CONTENT_TYPES.get(readme_path.suffix.lower(), "text/plain")


def _parse_readme(
    project_root: pathlib.Path,
    raw_readme: str | Mapping[str, Any] | None,
) -> Readme | None:
    if raw_readme is None:
        return None
    if isinstance(raw_readme, str):
        readme_path = project_root / raw_readme
        return Readme(
            text=readme_path.read_text(encoding="utf-8"),
            content_type=_infer_readme_content_type(readme_path),
        )
    if isinstance(raw_readme, Mapping):
        explicit_ct = raw_readme.get("content-type")
        if "file" in raw_readme:
            readme_path = project_root / str(raw_readme["file"])
            content_type = (
                str(explicit_ct)
                if explicit_ct is not None
                else _infer_readme_content_type(readme_path)
            )
            return Readme(
                text=readme_path.read_text(encoding="utf-8"),
                content_type=content_type,
            )
        if "text" in raw_readme:
            content_type = str(explicit_ct) if explicit_ct is not None else "text/plain"
            return Readme(
                text=str(raw_readme["text"]),
                content_type=content_type,
            )
    raise ValueError(f"Unsupported [project].readme value: {raw_readme!r}")


def _parse_project_metadata(
    project_root: pathlib.Path,
    project_table: Mapping[str, Any],
) -> ProjectMetadata:
    if "name" not in project_table:
        raise ValueError("Missing required [project].name in pyproject.toml")
    if "version" not in project_table:
        raise ValueError("Missing required [project].version in pyproject.toml")

    normalized_version = version_filename_component(str(project_table["version"]))
    readme = _parse_readme(project_root, project_table.get("readme"))

    license_expr: str | None = None
    license_text: str | None = None
    license_files = list(project_table.get("license-files", ()))
    raw_license = project_table.get("license")
    if isinstance(raw_license, str):
        license_expr = raw_license
    elif isinstance(raw_license, Mapping):
        if "text" in raw_license:
            license_text = str(raw_license["text"])
        elif "file" in raw_license:
            lic_file = str(raw_license["file"])
            if lic_file not in license_files:
                license_files.append(lic_file)

    opt_deps = types.MappingProxyType(
        {
            str(group): tuple(str(dep) for dep in deps)
            for group, deps in project_table.get("optional-dependencies", {}).items()
        }
    )

    return ProjectMetadata(
        name=str(project_table["name"]),
        version=normalized_version,
        description=str(project_table.get("description", "")),
        readme=readme,
        requires_python=(
            str(project_table["requires-python"])
            if "requires-python" in project_table
            else None
        ),
        license_expression=license_expr,
        license_text=license_text,
        license_files=tuple(str(f) for f in license_files),
        authors=tuple(
            types.MappingProxyType({str(k): str(v) for k, v in a.items()})
            for a in project_table.get("authors", ())
        ),
        maintainers=tuple(
            types.MappingProxyType({str(k): str(v) for k, v in m.items()})
            for m in project_table.get("maintainers", ())
        ),
        keywords=tuple(str(k) for k in project_table.get("keywords", ())),
        classifiers=tuple(str(c) for c in project_table.get("classifiers", ())),
        urls=types.MappingProxyType(
            {str(k): str(v) for k, v in project_table.get("urls", {}).items()}
        ),
        dependencies=tuple(str(d) for d in project_table.get("dependencies", ())),
        optional_dependencies=opt_deps,
    )


def _parse_bazel_config(
    tool_table: Mapping[str, Any],
    config_settings: Mapping[str, Any] | None = None,
) -> BazelConfig:
    cs = config_settings or _EMPTY_STR_MAP
    raw_target = (
        cs.get("target")
        or cs.get("wheel-target")
        or tool_table.get("target")
        or tool_table.get("wheel-target")
    )
    if not raw_target:
        raise ValueError(
            'Missing required Bazel wheel target: set `target = "//..."` '
            "in [tool.bazel517] or pass `-C target=//...`."
        )

    raw_bazel_bin = (
        cs.get("bazel-bin")
        or os.environ.get("BAZEL_BIN")
        or tool_table.get("bazel-bin")
    )
    bazel_bin = str(raw_bazel_bin) if raw_bazel_bin else None

    template_vars = python_template_vars()
    base_args = tuple(
        interpolate_arg(str(a), template_vars) for a in tool_table.get("bazel-args", ())
    )
    editable_args = tuple(
        interpolate_arg(str(a), template_vars)
        for a in tool_table.get(
            "editable-bazel-args",
            tool_table.get("bazel-args", ()),
        )
    )

    env_args = tuple(shlex.split(os.environ.get("BAZEL517_BAZEL_ARGS", "")))
    cs_args = tuple(
        interpolate_arg(a, template_vars)
        for a in _extract_config_setting_list(
            config_settings,
            "bazel-args",
            "bazel_args",
        )
    )

    env_map = types.MappingProxyType(
        {
            str(k): interpolate_arg(str(v), template_vars)
            for k, v in tool_table.get("env", {}).items()
        }
    )

    return BazelConfig(
        target=str(raw_target),
        bazel_bin=bazel_bin,
        bazel_args=(*base_args, *env_args, *cs_args),
        editable_bazel_args=(*editable_args, *env_args, *cs_args),
        env=env_map,
    )


def _read_pyproject_toml(
    project_root: pathlib.Path | None,
) -> tuple[pathlib.Path, Mapping[str, Any]]:
    root = (project_root or pathlib.Path.cwd()).resolve()
    pyproject_path = root / "pyproject.toml"
    if not pyproject_path.is_file():
        raise FileNotFoundError(f"pyproject.toml not found in {root}")
    with pyproject_path.open("rb") as f:
        data = tomllib.load(f)
    return root, data


def load_wheel_config(
    project_root: pathlib.Path | None = None,
    config_settings: Mapping[str, Any] | None = None,
) -> WheelBuildConfig:
    """Load `[tool.bazel517]` configuration for building wheels.

    Does not require static `[project].name` or `[project].version` fields in
    `pyproject.toml`, allowing projects whose version is stamped dynamically by
    Bazel to build standard and editable wheels.
    """
    root, data = _read_pyproject_toml(project_root)
    tool_table = data.get("tool", {}).get("bazel517", {})
    if not isinstance(tool_table, Mapping):
        raise ValueError("[tool.bazel517] must be a TOML table")
    bazel_cfg = _parse_bazel_config(tool_table, config_settings)
    return WheelBuildConfig(project_root=root, bazel=bazel_cfg)


def load_sdist_config(
    project_root: pathlib.Path | None = None,
) -> SdistBuildConfig:
    """Load `[project]` metadata from `pyproject.toml` for building an sdist."""
    root, data = _read_pyproject_toml(project_root)
    project_table = data.get("project")
    if not isinstance(project_table, Mapping):
        raise ValueError("Missing [project] table in pyproject.toml")
    metadata = _parse_project_metadata(root, project_table)
    return SdistBuildConfig(project_root=root, metadata=metadata)
