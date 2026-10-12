"""Bazel wheel target building and source-layout discovery."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import dataclasses
import json
import os
import pathlib
import subprocess

from . import _config


class BazelError(RuntimeError):
    """Raised when a Bazel command fails or produces unexpected output."""


@dataclasses.dataclass(frozen=True, slots=True)
class WheelSourceLayout:
    """Source file layout extracted directly from Bazel's `PyWheel` action."""

    source_arcnames: frozenset[str]
    pth_dirs: tuple[pathlib.Path, ...]


def _merged_env(extra_env: Mapping[str, str]) -> dict[str, str]:
    env = dict(os.environ)
    env.update(extra_env)
    return env


def build_bazel_wheel_target(
    project_root: pathlib.Path,
    bazel_cfg: _config.BazelConfig,
    *,
    editable: bool = False,
) -> pathlib.Path:
    """Build `bazel_cfg.target` and return the path to the generated `.whl` file."""
    flags = bazel_cfg.editable_bazel_args if editable else bazel_cfg.bazel_args
    env = _merged_env(bazel_cfg.env)

    build_cmd = [bazel_cfg.bazel_bin, "build", *flags, bazel_cfg.target]
    subprocess.run(
        build_cmd,
        cwd=project_root,
        env=env,
        check=True,
    )

    cquery_cmd = [
        bazel_cfg.bazel_bin,
        "cquery",
        "--output=files",
        "--ui_event_filters=-info,-stderr",
        "--noshow_progress",
        *flags,
        bazel_cfg.target,
    ]
    proc = subprocess.run(
        cquery_cmd,
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    whl_paths: list[pathlib.Path] = []
    for raw_line in proc.stdout.splitlines():
        line = raw_line.strip()
        if not line:
            continue
        candidate = pathlib.Path(line)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        if candidate.suffix == ".whl":
            whl_paths.append(candidate)

    if len(whl_paths) != 1:
        raise BazelError(
            f"Expected Bazel target {bazel_cfg.target!r} to produce "
            f"exactly one .whl file, got: {[str(p) for p in whl_paths]}"
        )
    if not whl_paths[0].is_file():
        raise BazelError(
            f"Bazel reported wheel output at {whl_paths[0]}, "
            "but the file does not exist."
        )
    return whl_paths[0]


def _arcname_from(
    package_filename: str,
    strip_path_prefixes: Sequence[str],
) -> str:
    """Compute the archive path inside the wheel, matching `rules_python` wheelmaker."""
    norm = package_filename.replace(os.path.sep, "/")
    for prefix in strip_path_prefixes:
        if norm.startswith(prefix):
            return norm[len(prefix) :].lstrip("/")
    return norm.lstrip("/")


def _parse_pywheel_action_args(
    arguments: Sequence[str],
) -> tuple[tuple[str, ...], tuple[str, ...], tuple[str, ...]]:
    """Extract strip prefixes and input file specs from `wheelmaker.py` argv."""
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--strip_path_prefix", action="append", default=[])
    parser.add_argument("--input_file_list", action="append", default=[])
    parser.add_argument("--input_file", action="append", default=[])
    known, _ = parser.parse_known_args(list(arguments[1:]))
    return (
        tuple(known.strip_path_prefix),
        tuple(known.input_file_list),
        tuple(known.input_file),
    )


def query_wheel_source_layout(
    project_root: pathlib.Path,
    bazel_cfg: _config.BazelConfig,
    *,
    editable: bool = True,
) -> WheelSourceLayout:
    """Query Bazel's `PyWheel` action for `bazel_cfg.target` to resolve source paths.

    Inspects the `PyWheel` action via `bazel aquery --output=jsonproto` to read
    `--strip_path_prefix`, `--input_file_list`, and `--input_file` directly from
    Bazel without hardcoding any directory layout (such as `src/`).
    """
    flags = bazel_cfg.editable_bazel_args if editable else bazel_cfg.bazel_args
    env = _merged_env(bazel_cfg.env)

    aquery_cmd = [
        bazel_cfg.bazel_bin,
        "aquery",
        f'mnemonic("PyWheel", {bazel_cfg.target})',
        "--output=jsonproto",
        "--ui_event_filters=-info,-stderr",
        "--noshow_progress",
        *flags,
    ]
    proc = subprocess.run(
        aquery_cmd,
        cwd=project_root,
        env=env,
        capture_output=True,
        text=True,
        check=True,
    )

    payload = json.loads(proc.stdout)
    actions = payload.get("actions", [])
    if not actions:
        msg = (
            f"No PyWheel action found on target {bazel_cfg.target!r} "
            "via `bazel aquery`."
        )
        raise BazelError(msg)

    strip_prefixes: list[str] = []
    input_file_lists: list[str] = []
    raw_pairs: list[str] = []

    for action in actions:
        argv = action.get("arguments", [])
        if not argv:
            continue
        prefixes, file_lists, inline_files = _parse_pywheel_action_args(argv)
        strip_prefixes.extend(prefixes)
        input_file_lists.extend(file_lists)
        raw_pairs.extend(inline_files)

    for list_rel in input_file_lists:
        list_path = pathlib.Path(list_rel)
        if not list_path.is_absolute():
            list_path = project_root / list_path
        if not list_path.is_file():
            raise BazelError(
                f"PyWheel input_file_list {list_path} does not exist on disk."
            )
        for line in list_path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                raw_pairs.append(line.strip())

    source_arcnames: set[str] = set()
    pth_dirs: list[pathlib.Path] = []
    seen_pth: set[pathlib.Path] = set()

    for entry in raw_pairs:
        if ";" not in entry:
            continue
        pkg_filename, real_filename = entry.split(";", maxsplit=1)
        real_posix = real_filename.replace(os.path.sep, "/")

        if real_posix.startswith(("bazel-out/", "external/")):
            continue
        src_file = project_root / real_posix
        if not src_file.is_file():
            continue

        arcname = _arcname_from(pkg_filename, strip_prefixes)
        source_arcnames.add(arcname)

        if real_posix == arcname:
            root_dir = project_root.resolve()
        elif real_posix.endswith("/" + arcname):
            prefix = real_posix[: -(len(arcname) + 1)]
            root_dir = (project_root / prefix).resolve()
        else:
            continue

        if root_dir not in seen_pth:
            seen_pth.add(root_dir)
            pth_dirs.append(root_dir)

    return WheelSourceLayout(
        source_arcnames=frozenset(source_arcnames),
        pth_dirs=tuple(pth_dirs),
    )
