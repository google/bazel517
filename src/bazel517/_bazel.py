"""Bazel wheel target building, binary resolution, and source-layout discovery."""

from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
import dataclasses
import hashlib
import json
import os
import pathlib
import platform
import shlex
import shutil
import stat
import subprocess
import sys
import tempfile
import types
from typing import Any, Final
import urllib.parse
import urllib.request

from . import _config

_BAZELISK_VERSION: Final[str] = "v1.29.0"
_DEFAULT_BAZELISK_BASE_URL: Final[str] = (
    f"https://github.com/bazelbuild/bazelisk/releases/download/{_BAZELISK_VERSION}"
)
_OS_ALIASES: Final[Mapping[str, str]] = types.MappingProxyType(
    {
        "linux": "linux",
        "darwin": "darwin",
        "win32": "windows",
    }
)
_ARCH_ALIASES: Final[Mapping[str, str]] = types.MappingProxyType(
    {
        "x86_64": "amd64",
        "amd64": "amd64",
        "aarch64": "arm64",
        "arm64": "arm64",
    }
)
_BAZELISK_ASSETS: Final[Mapping[tuple[str, str], tuple[str, str]]] = (
    types.MappingProxyType(
        {
            ("linux", "amd64"): (
                "bazelisk-linux-amd64",
                "5a408715e932c0250d28bd84555f12edbf70117de42f9181691c736eacc4a992",
            ),
            ("linux", "arm64"): (
                "bazelisk-linux-arm64",
                "e20e8b0f4f240091b7a55bf17b9398bd4f40ee70ae0208dff95dd4c445fb4010",
            ),
            ("darwin", "amd64"): (
                "bazelisk-darwin-amd64",
                "16c3d7aa15323a9fb69f56c7ec5733ed18bedb786680d0ba13bb12a3c8083007",
            ),
            ("darwin", "arm64"): (
                "bazelisk-darwin-arm64",
                "cee851f726789227d5561004e9904a52be45c3efb56f8b38b6993d6adbaa0409",
            ),
            ("windows", "amd64"): (
                "bazelisk-windows-amd64.exe",
                "092a8738d5b41aae7a85c42cc961b1034e3389aba43ffc20c0fabda7b43e095b",
            ),
            ("windows", "arm64"): (
                "bazelisk-windows-arm64.exe",
                "8bc42bd5d7857f18a21440b906469bb6c7cf91a7c72364d4b1e5ec56a76fe94f",
            ),
        }
    )
)


class BazelError(RuntimeError):
    """Raised when a Bazel command fails or produces unexpected output."""


@dataclasses.dataclass(frozen=True, slots=True)
class WheelBuildResult:
    """Output wheel path and canonicalized build flags from `bazel build`."""

    wheel_path: pathlib.Path
    expanded_args: tuple[str, ...] | None = None


@dataclasses.dataclass(frozen=True, slots=True)
class WheelSourceLayout:
    """Source file layout extracted directly from Bazel's `PyWheel` action."""

    source_arcnames: frozenset[str]
    pth_dirs: tuple[pathlib.Path, ...]


def _merged_env(extra_env: Mapping[str, str]) -> Mapping[str, str]:
    env = dict(os.environ)
    env.update(extra_env)
    return env


def _bazelisk_cache_dir() -> pathlib.Path:
    if custom := os.environ.get("BAZEL517_CACHE_DIR"):
        return pathlib.Path(custom)
    if xdg := os.environ.get("XDG_CACHE_HOME"):
        return pathlib.Path(xdg) / "bazel517"
    return pathlib.Path.home() / ".cache" / "bazel517"


def _download_bazelisk() -> str:
    os_key = _OS_ALIASES.get(sys.platform, "")
    arch_key = _ARCH_ALIASES.get(platform.machine().lower(), "")
    asset = _BAZELISK_ASSETS.get((os_key, arch_key))
    if asset is None:
        raise BazelError(
            "Neither `bazelisk` nor `bazel` was found on PATH, and no prebuilt "
            f"bazelisk binary is available for {sys.platform}/{platform.machine()}."
        )
    asset_name, default_sha256 = asset
    expected_sha256 = os.environ.get("BAZEL517_BAZELISK_SHA256", default_sha256).lower()

    dest_dir = _bazelisk_cache_dir() / f"bazelisk-{_BAZELISK_VERSION}"
    dest_path = dest_dir / asset_name
    if (
        dest_path.is_file()
        and hashlib.sha256(dest_path.read_bytes()).hexdigest() == expected_sha256
    ):
        return str(dest_path)

    base_url = os.environ.get(
        "BAZEL517_BAZELISK_BASE_URL", _DEFAULT_BAZELISK_BASE_URL
    ).rstrip("/")
    url = f"{base_url}/{asset_name}"
    try:
        with urllib.request.urlopen(url, timeout=60) as resp:
            data = resp.read()
    except Exception as exc:
        raise BazelError(f"Failed to download bazelisk from {url}: {exc}") from exc

    actual_sha256 = hashlib.sha256(data).hexdigest()
    if actual_sha256 != expected_sha256:
        raise BazelError(
            f"SHA-256 mismatch for bazelisk downloaded from {url}: "
            f"expected {expected_sha256}, got {actual_sha256}"
        )

    dest_dir.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(dir=dest_dir, delete=False) as tmp_file:
        tmp_file.write(data)
        tmp_path = pathlib.Path(tmp_file.name)
    tmp_path.chmod(tmp_path.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    tmp_path.replace(dest_path)
    return str(dest_path)


def _resolve_bazel_bin(configured_bin: str | None) -> str:
    """Resolve the Bazel executable to invoke.

    Uses an explicitly configured binary first; otherwise prefers `bazelisk`
    over `bazel` on `PATH`, and falls back to downloading a pinned `bazelisk`
    release binary verified by SHA-256.
    """
    if configured_bin is not None:
        return configured_bin
    found = shutil.which("bazelisk") or shutil.which("bazel")
    if found is not None:
        return found
    return _download_bazelisk()


def _path_from_bep_file_entry(
    project_root: pathlib.Path,
    file_entry: Mapping[str, Any],
) -> pathlib.Path | None:
    uri = file_entry.get("uri")
    if isinstance(uri, str) and uri.startswith("file://"):
        parsed = urllib.parse.urlparse(uri)
        return pathlib.Path(urllib.request.url2pathname(parsed.path))
    name = file_entry.get("name")
    if isinstance(name, str) and name:
        prefix = [str(p) for p in file_entry.get("pathPrefix", ())]
        candidate = pathlib.Path(*prefix, name)
        if not candidate.is_absolute():
            candidate = project_root / candidate
        return candidate
    return None


def _parse_bep_events(
    project_root: pathlib.Path,
    bep_path: pathlib.Path,
) -> tuple[Sequence[pathlib.Path], tuple[str, ...] | None]:
    whl_paths: list[pathlib.Path] = []
    expanded_args: tuple[str, ...] | None = None

    for raw_line in bep_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line:
            continue
        event = json.loads(line)

        for file_entry in event.get("namedSetOfFiles", {}).get("files", ()):
            candidate = _path_from_bep_file_entry(project_root, file_entry)
            if (
                candidate is not None
                and candidate.suffix == ".whl"
                and candidate not in whl_paths
            ):
                whl_paths.append(candidate)

        cmd_label = (
            event.get("id", {}).get("structuredCommandLine", {}).get("commandLineLabel")
        )
        if cmd_label == "canonical":
            expanded: list[str] = []
            for section in event.get("structuredCommandLine", {}).get("sections", ()):
                if section.get("sectionLabel") != "command options":
                    continue
                for opt in section.get("optionList", {}).get("option", ()):
                    tags = set(opt.get("metadataTags", ()))
                    if "HIDDEN" in tags or "NON_CONFIGURABLE" in tags:
                        continue
                    if opt.get("optionName") in {"config", "build_event_json_file"}:
                        continue
                    source = str(opt.get("source", ""))
                    if (
                        not source
                        or source == "command line options"
                        or source.startswith("expanded from --config=")
                    ):
                        expanded.extend(shlex.split(str(opt.get("combinedForm", ""))))
            expanded_args = tuple(expanded)

    return tuple(whl_paths), expanded_args


def build_bazel_wheel_target(
    project_root: pathlib.Path,
    bazel_cfg: _config.BazelConfig,
    *,
    editable: bool = False,
) -> WheelBuildResult:
    """Build `bazel_cfg.target` and return the `.whl` path and expanded build flags.

    Uses `--build_event_json_file` (Build Event Protocol) on `bazel build` so
    that the `.whl` output path and any `build:`-scoped `.bazelrc` `--config`
    expansions are captured directly from `bazel build` in a single invocation
    without running `bazel cquery`.
    """
    bazel_bin = _resolve_bazel_bin(bazel_cfg.bazel_bin)
    flags = bazel_cfg.editable_bazel_args if editable else bazel_cfg.bazel_args
    env = _merged_env(bazel_cfg.env)

    with tempfile.TemporaryDirectory() as tmp_dir:
        bep_path = pathlib.Path(tmp_dir) / "bep.json"
        build_cmd = [
            bazel_bin,
            "build",
            *flags,
            f"--build_event_json_file={bep_path}",
            bazel_cfg.target,
        ]
        subprocess.run(
            build_cmd,
            cwd=project_root,
            env=env,
            check=True,
        )
        whl_paths, expanded_args = _parse_bep_events(project_root, bep_path)

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
    return WheelBuildResult(
        wheel_path=whl_paths[0],
        expanded_args=expanded_args,
    )


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
) -> tuple[Sequence[str], Sequence[str], Sequence[str]]:
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
    expanded_args: Sequence[str] | None = None,
) -> WheelSourceLayout:
    """Query Bazel's `PyWheel` action for `bazel_cfg.target` to resolve source paths.

    Inspects the `PyWheel` action via `bazel aquery --output=jsonproto` to read
    `--strip_path_prefix`, `--input_file_list`, and `--input_file` directly from
    Bazel without hardcoding any directory layout (such as `src/`). When
    `expanded_args` is provided from `bazel build`'s Build Event Protocol output,
    those already-expanded flags are passed instead of raw `--config` flags so
    `build:`-scoped `.bazelrc` definitions cannot diverge in `aquery`.
    """
    bazel_bin = _resolve_bazel_bin(bazel_cfg.bazel_bin)
    if expanded_args is not None:
        flags: Sequence[str] = expanded_args
    else:
        flags = bazel_cfg.editable_bazel_args if editable else bazel_cfg.bazel_args
    env = _merged_env(bazel_cfg.env)

    aquery_cmd = [
        bazel_bin,
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
