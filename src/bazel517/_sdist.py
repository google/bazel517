"""Source distribution (sdist) builder implementing PEP 517 / PEP 625."""

from __future__ import annotations

from collections.abc import Sequence
import io
import pathlib
import subprocess
import tarfile
from typing import Final

from . import _config, _metadata

_EXCLUDED_DIRS: Final[frozenset[str]] = frozenset(
    {
        ".git",
        ".hg",
        ".jj",
        ".nox",
        ".tox",
        ".venv",
        "venv",
        ".mypy_cache",
        ".pytest_cache",
        ".ruff_cache",
        "__pycache__",
        "build",
        "dist",
    }
)


def _git_tracked_files(
    project_root: pathlib.Path,
) -> Sequence[pathlib.Path] | None:
    if not (project_root / ".git").exists():
        return None
    try:
        proc = subprocess.run(
            ["git", "ls-files", "-z", "--recurse-submodules"],
            cwd=project_root,
            capture_output=True,
            check=True,
        )
    except FileNotFoundError:
        return None

    files: list[pathlib.Path] = []
    for raw in proc.stdout.split(b"\0"):
        if not raw:
            continue
        p = project_root / raw.decode("utf-8")
        if p.is_file():
            files.append(p)
    return tuple(sorted(files))


def _fallback_walk_files(
    project_root: pathlib.Path,
) -> Sequence[pathlib.Path]:
    files: list[pathlib.Path] = []
    for path in sorted(project_root.rglob("*")):
        if not path.is_file():
            continue
        rel_parts = path.relative_to(project_root).parts
        if any(
            part in _EXCLUDED_DIRS or part.startswith("bazel-") for part in rel_parts
        ):
            continue
        if path.suffix in {".pyc", ".pyo"}:
            continue
        files.append(path)
    return tuple(files)


def build_source_distribution(
    cfg: _config.BuildConfig,
    sdist_dir: pathlib.Path,
) -> str:
    """Build a PEP 625 `.tar.gz` source distribution in `sdist_dir`."""
    norm_name = _config.distribution_filename_component(cfg.metadata.name)
    norm_ver = _config.version_filename_component(cfg.metadata.version)
    base_prefix = f"{norm_name}-{norm_ver}"
    sdist_name = f"{base_prefix}.tar.gz"

    sdist_dir.mkdir(parents=True, exist_ok=True)
    sdist_path = sdist_dir / sdist_name

    files = _git_tracked_files(cfg.project_root) or _fallback_walk_files(
        cfg.project_root
    )

    with tarfile.open(sdist_path, mode="w:gz", format=tarfile.PAX_FORMAT) as tf:
        for src_path in files:
            rel_posix = src_path.relative_to(cfg.project_root).as_posix()
            tf.add(src_path, arcname=f"{base_prefix}/{rel_posix}", recursive=False)

        pkg_info_bytes = _metadata.format_core_metadata(cfg.metadata).encode("utf-8")
        info = tarfile.TarInfo(name=f"{base_prefix}/PKG-INFO")
        info.size = len(pkg_info_bytes)
        info.mode = 0o644
        tf.addfile(info, io.BytesIO(pkg_info_bytes))

    return sdist_name
