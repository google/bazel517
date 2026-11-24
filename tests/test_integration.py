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

"""End-to-end integration tests running real Bazel versions and bazelisk."""

from __future__ import annotations

from collections.abc import Iterator, Mapping, Sequence
import contextlib
import dataclasses
import enum
import json
import os
import pathlib
import shutil
import subprocess
import sys
import textwrap
from typing import Final
import zipfile

import pytest

import bazel517
from bazel517 import _bazel
import conftest

# Kept in sync with `.github/workflows/ci.yml` (`actions/cache` key); verified
# by `test_ci_cache_matches_bazel_versions`.
_BAZEL_VERSIONS: Final[tuple[str, str]] = ("7.4.1", "8.6.0")
_REPO_ROOT: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parents[1]
_BAZEL_WRAPPER_SCRIPT: Final[pathlib.Path] = (
    pathlib.Path(__file__).resolve().parent / "data" / "bazel_wrapper.py"
)


class _VersionSource(enum.StrEnum):
    BAZELVERSION = "bazelversion"
    BAZELVERSION_FLOATING = "bazelversion_floating"
    BAZELISKRC = "bazeliskrc"


class _InstalledKind(enum.StrEnum):
    STANDALONE = "standalone"
    WRAPPER = "wrapper"


@dataclasses.dataclass(frozen=True, slots=True)
class _RealBazelEnv:
    """Paths and workspaces for real multi-version Bazel integration tests."""

    bazelisk_bin: pathlib.Path
    bazelisk_home: pathlib.Path
    bazel_bins: Mapping[str, pathlib.Path]
    workspaces: Mapping[str, pathlib.Path]
    repo_cache: pathlib.Path
    clean_system_path: str


def _build_clean_system_path(shadow_root: pathlib.Path) -> str:
    """Return a PATH with all system tools preserved but host bazel/bazelisk removed."""
    new_parts: list[str] = []
    for idx, raw_dir in enumerate(os.environ.get("PATH", "").split(os.pathsep)):
        if not raw_dir:
            continue
        p = pathlib.Path(raw_dir)
        if not p.is_dir():
            continue
        has_bazel = any(
            (p / name).exists()
            for name in ("bazel", "bazel.exe", "bazelisk", "bazelisk.exe")
        )
        if not has_bazel:
            new_parts.append(raw_dir)
            continue
        if sys.platform == "win32":
            # On Windows runners, bazel/bazelisk live in Chocolatey/toolcache dirs
            # rather than C:\Windows\System32, so omitting the directory hides them.
            continue
        shadow_dir = shadow_root / f"path_{idx}"
        shadow_dir.mkdir(parents=True, exist_ok=True)
        for item in p.iterdir():
            if item.name.lower().startswith("bazel"):
                continue
            with contextlib.suppress(OSError):
                (shadow_dir / item.name).symlink_to(item)
        new_parts.append(str(shadow_dir))
    return os.pathsep.join(new_parts)


def _ensure_standalone_bazel(
    version: str,
    bazelisk_bin: pathlib.Path,
    bazelisk_home: pathlib.Path,
) -> pathlib.Path:
    """Ensure `version` is downloaded in `bazelisk_home` and return its binary path."""
    exe_suffix = ".exe" if sys.platform == "win32" else ""
    version_dir = bazelisk_home / f"v_{version}"
    version_dir.mkdir(parents=True, exist_ok=True)

    env = dict(os.environ)
    env["USE_BAZEL_VERSION"] = version
    env["BAZELISK_HOME"] = str(version_dir)
    subprocess.run(
        [str(bazelisk_bin), "--version"],
        env=env,
        check=True,
        capture_output=True,
        text=True,
    )
    for candidate in (version_dir / "downloads").rglob(f"bazel{exe_suffix}"):
        if candidate.is_file() and not candidate.name.endswith(".lock"):
            return candidate
    raise RuntimeError(f"Could not find downloaded Bazel {version} in {version_dir}")


def _write_bazel_workspace(workspace_dir: pathlib.Path) -> None:
    """Write a minimal real `rules_python` `py_wheel` workspace."""
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "build_info.bzl").write_text(
        textwrap.dedent(
            """\
            def _bazel_version_repo_impl(rctx):
                rctx.file("BUILD.bazel", "")
                rctx.file("defs.bzl", "BAZEL_VERSION = %r\\n" % (native.bazel_version,))

            bazel_version_repo = repository_rule(
                implementation = _bazel_version_repo_impl,
            )

            def _ext_impl(_mctx):
                bazel_version_repo(name = "bazel_version_info")

            bazel_version_ext = module_extension(implementation = _ext_impl)

            def _build_info_impl(ctx):
                req_prefix = ctx.var.get("REQUIRED_BAZEL_PREFIX", "")
                if req_prefix and not ctx.attr.bazel_version.startswith(req_prefix):
                    fail(
                        "Workspace .bazelrc requires Bazel %s*, but active Bazel is %s"
                        % (req_prefix, ctx.attr.bazel_version)
                    )
                out = ctx.actions.declare_file(ctx.attr.out_name)
                tag = ctx.var.get("BAZELRC_TAG", "unset")
                ctx.actions.write(
                    output = out,
                    content = "BAZEL_VERSION = %r\\nBAZELRC_TAG = %r\\n"
                    % (ctx.attr.bazel_version, tag),
                )
                return [DefaultInfo(files = depset([out]))]

            build_info = rule(
                implementation = _build_info_impl,
                attrs = {
                    "out_name": attr.string(mandatory = True),
                    "bazel_version": attr.string(mandatory = True),
                },
            )
            """
        ),
        encoding="utf-8",
    )
    (workspace_dir / "MODULE.bazel").write_text(
        textwrap.dedent(
            """\
            module(name = "sample_pkg", version = "0.1.0")
            bazel_dep(name = "rules_python", version = "1.2.0")

            bazel_version_ext = use_extension("//:build_info.bzl", "bazel_version_ext")
            use_repo(bazel_version_ext, "bazel_version_info")
            """
        ),
        encoding="utf-8",
    )
    (workspace_dir / "BUILD.bazel").write_text(
        textwrap.dedent(
            """\
            load("@rules_python//python:packaging.bzl", "py_wheel")
            load("@rules_python//python:defs.bzl", "py_library")
            load("@bazel_version_info//:defs.bzl", "BAZEL_VERSION")
            load("//:build_info.bzl", "build_info")

            build_info(
                name = "gen_build_info",
                out_name = "src/sample_pkg/_build_info.py",
                bazel_version = BAZEL_VERSION,
            )

            py_library(
                name = "sample_lib",
                srcs = [
                    "src/sample_pkg/__init__.py",
                    ":gen_build_info",
                ],
                imports = ["src"],
            )

            py_wheel(
                name = "sample_wheel",
                distribution = "sample_pkg",
                python_tag = "py3",
                strip_path_prefixes = ["src"],
                version = "0.1.0",
                deps = [":sample_lib"],
            )
            """
        ),
        encoding="utf-8",
    )
    pkg_dir = workspace_dir / "src" / "sample_pkg"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text(
        textwrap.dedent(
            """\
            from ._build_info import BAZEL_VERSION, BAZELRC_TAG

            LIVE_VALUE = "initial"
            """
        ),
        encoding="utf-8",
    )
    (workspace_dir / "pyproject.toml").write_text(
        textwrap.dedent(
            """\
            [build-system]
            requires = ["bazel517"]
            build-backend = "bazel517"

            [project]
            name = "sample-pkg"
            version = "0.1.0"

            [tool.bazel517]
            target = "//:sample_wheel"
            editable-bazel-args = ["--config=editable"]
            """
        ),
        encoding="utf-8",
    )


def _install_bazel_wrapper(
    bin_dir: pathlib.Path,
    installed_bins: Mapping[str, pathlib.Path],
    default_version: str,
) -> None:
    """Install a multi-version `bazel` wrapper executable into `bin_dir`."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    config_path = bin_dir / "_bazel_wrapper_config.json"
    config_payload = {
        "installed": {ver: str(path) for ver, path in installed_bins.items()},
        "default_version": default_version,
    }
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")
    conftest.build_python_shim(
        bin_dir=bin_dir,
        name="bazel",
        script_path=_BAZEL_WRAPPER_SCRIPT,
        config_path=config_path,
    )


@pytest.fixture(scope="session")
def real_bazel_env(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[_RealBazelEnv]:
    root = tmp_path_factory.mktemp("real_bazel")
    bazelisk_bin = pathlib.Path(_bazel._download_bazelisk(os.environ))
    bazelisk_home = (
        pathlib.Path(
            os.environ.get(
                "BAZEL517_TEST_BAZELISK_HOME",
                str(pathlib.Path.home() / ".cache" / "bazel517-test-bazelisk"),
            )
        )
        .expanduser()
        .resolve()
    )
    repo_cache = (
        pathlib.Path(
            os.environ.get(
                "BAZEL517_TEST_REPO_CACHE",
                str(pathlib.Path.home() / ".cache" / "bazel517-test-repo-cache"),
            )
        )
        .expanduser()
        .resolve()
    )
    repo_cache.mkdir(parents=True, exist_ok=True)

    bazel_bins: dict[str, pathlib.Path] = {}
    workspaces: dict[str, pathlib.Path] = {}
    for ver in _BAZEL_VERSIONS:
        bazel_bins[ver] = _ensure_standalone_bazel(ver, bazelisk_bin, bazelisk_home)
        ws = root / f"ws_{ver}"
        _write_bazel_workspace(ws)
        workspaces[ver] = ws

    clean_path = _build_clean_system_path(root / "clean_path")

    try:
        yield _RealBazelEnv(
            bazelisk_bin=bazelisk_bin,
            bazelisk_home=bazelisk_home,
            bazel_bins=bazel_bins,
            workspaces=workspaces,
            repo_cache=repo_cache,
            clean_system_path=clean_path,
        )
    finally:
        # Match `--output_user_root=C:/b` from `.bazelrc` on Windows (used to
        # keep Bazel external repository paths under Windows `MAX_PATH`).
        startup_args = ["--output_user_root=C:/b"] if sys.platform == "win32" else []
        for ver, ws in workspaces.items():
            subprocess.run(
                [str(bazel_bins[ver]), *startup_args, "shutdown"],
                cwd=ws,
                check=False,
                capture_output=True,
            )


def _configure_workspace(
    ws: pathlib.Path,
    repo_cache: pathlib.Path,
    *,
    bazelversion_text: str | None,
    bazeliskrc_text: str | None,
    required_major_minor: str,
    bazelrc_tag: str,
) -> None:
    for rc_file in (ws / ".bazelversion", ws / ".bazeliskrc"):
        rc_file.unlink(missing_ok=True)
    if bazelversion_text is not None:
        (ws / ".bazelversion").write_text(bazelversion_text, encoding="utf-8")
    if bazeliskrc_text is not None:
        (ws / ".bazeliskrc").write_text(bazeliskrc_text, encoding="utf-8")

    # Use a short `output_user_root` on Windows so external repository paths
    # under Bazel's execroot stay below the 260-character `MAX_PATH` limit.
    win_startup = "startup --output_user_root=C:/b\n" if sys.platform == "win32" else ""
    (ws / ".bazelrc").write_text(
        f"{win_startup}"
        f"common --repository_cache={repo_cache.as_posix()}\n"
        "common --check_direct_dependencies=off\n"
        "common --incompatible_strict_action_env=true\n"
        f"build --define=REQUIRED_BAZEL_PREFIX={required_major_minor}.\n"
        f"build --define=BAZELRC_TAG={bazelrc_tag}\n"
        f"build:editable --define=BAZELRC_TAG={bazelrc_tag}_editable\n",
        encoding="utf-8",
    )
    (ws / "src" / "sample_pkg" / "__init__.py").write_text(
        textwrap.dedent(
            """\
            from ._build_info import BAZEL_VERSION, BAZELRC_TAG

            LIVE_VALUE = "initial"
            """
        ),
        encoding="utf-8",
    )


def _verify_built_wheel(
    whl_path: pathlib.Path,
    unpack_dir: pathlib.Path,
    *,
    expected_bazel_version: str,
    expected_bazelrc_tag: str,
) -> None:
    with zipfile.ZipFile(whl_path) as zf:
        zf.extractall(unpack_dir)
    check_script = textwrap.dedent(
        f"""\
        import json
        import site

        site.addsitedir({str(unpack_dir)!r})
        import sample_pkg

        print(
            json.dumps(
                {{
                    "bazel_version": sample_pkg.BAZEL_VERSION,
                    "bazelrc_tag": sample_pkg.BAZELRC_TAG,
                    "live_value": sample_pkg.LIVE_VALUE,
                }}
            )
        )
        """
    )
    check = subprocess.run(
        [sys.executable, "-I", "-c", check_script],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(check.stdout.strip())
    assert data["bazel_version"] == expected_bazel_version
    assert data["bazelrc_tag"] == expected_bazelrc_tag
    assert data["live_value"] == "initial"


def test_ci_cache_matches_bazel_versions() -> None:
    ci_yml = _REPO_ROOT / ".github" / "workflows" / "ci.yml"
    content = ci_yml.read_text(encoding="utf-8")
    expected_key = (
        f"key: bazel-int-${{{{ runner.os }}}}-"
        f"{_bazel._BAZELISK_VERSION}-{'-'.join(_BAZEL_VERSIONS)}"
    )
    assert "~/.cache/bazel517\n" in content
    assert expected_key in content


@pytest.mark.parametrize(
    (
        "required_version",
        "version_source",
        "installed_kind",
        "installed_versions",
        "expect_fallback_to_bazelisk",
    ),
    [
        # 1. Standalone `bazel` 7.4.1 on PATH matches `.bazelversion` 7.4.1
        (
            "7.4.1",
            _VersionSource.BAZELVERSION,
            _InstalledKind.STANDALONE,
            ("7.4.1",),
            False,
        ),
        # 2. Standalone `bazel` 7.4.1 on PATH does NOT match `.bazelversion` 8.6.0
        # -> skips PATH `bazel` 7.4.1 and falls back to bazelisk to run real Bazel 8.6.0
        (
            "8.6.0",
            _VersionSource.BAZELVERSION,
            _InstalledKind.STANDALONE,
            ("7.4.1",),
            True,
        ),
        # 3. Standalone `bazel` 8.6.0 on PATH satisfies floating `.bazelversion` `8.x`
        (
            "8.6.0",
            _VersionSource.BAZELVERSION_FLOATING,
            _InstalledKind.STANDALONE,
            ("8.6.0",),
            False,
        ),
        # 4. Standalone `bazel` 8.6.0 on PATH does NOT match `.bazeliskrc` (7.4.1)
        # -> falls back to bazelisk to run real Bazel 7.4.1
        (
            "7.4.1",
            _VersionSource.BAZELISKRC,
            _InstalledKind.STANDALONE,
            ("8.6.0",),
            True,
        ),
        # 5. Multi-version `bazel` wrapper on PATH with both 7.4.1 and 8.6.0 installed
        # dispatches directly to 7.4.1 when `.bazelversion` requires 7.4.1
        (
            "7.4.1",
            _VersionSource.BAZELVERSION,
            _InstalledKind.WRAPPER,
            ("7.4.1", "8.6.0"),
            False,
        ),
        # 6. Multi-version `bazel` wrapper on PATH with both 7.4.1 and 8.6.0 installed
        # dispatches directly to 8.6.0 when `.bazelversion` requires 8.6.0
        (
            "8.6.0",
            _VersionSource.BAZELVERSION,
            _InstalledKind.WRAPPER,
            ("7.4.1", "8.6.0"),
            False,
        ),
        # 7. Multi-version `bazel` wrapper on PATH with only 7.4.1 installed fails on
        # `.bazelversion` 8.6.0 -> falls back to bazelisk to run real Bazel 8.6.0
        (
            "8.6.0",
            _VersionSource.BAZELVERSION,
            _InstalledKind.WRAPPER,
            ("7.4.1",),
            True,
        ),
    ],
)
def test_real_bazel_wheel_and_editable_across_versions(
    real_bazel_env: _RealBazelEnv,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    required_version: str,
    version_source: _VersionSource,
    installed_kind: _InstalledKind,
    installed_versions: Sequence[str],
    expect_fallback_to_bazelisk: bool,
) -> None:
    ws = real_bazel_env.workspaces[required_version]
    major_minor = ".".join(required_version.split(".")[:2])
    tag = f"tag_{required_version.replace('.', '_')}_{installed_kind}"

    if version_source == _VersionSource.BAZELVERSION:
        bv_text: str | None = f"{required_version}\n"
        brc_text: str | None = None
    elif version_source == _VersionSource.BAZELVERSION_FLOATING:
        major = required_version.split(".")[0]
        bv_text = f"{major}.x\n"
        brc_text = None
    elif version_source == _VersionSource.BAZELISKRC:
        bv_text = None
        brc_text = f"USE_BAZEL_VERSION={required_version}\n"
    else:
        raise ValueError(f"Unsupported version_source: {version_source!r}")

    _configure_workspace(
        ws,
        real_bazel_env.repo_cache,
        bazelversion_text=bv_text,
        bazeliskrc_text=brc_text,
        required_major_minor=major_minor,
        bazelrc_tag=tag,
    )

    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    exe_suffix = ".exe" if sys.platform == "win32" else ""
    if installed_kind == _InstalledKind.STANDALONE:
        only_ver = installed_versions[0]
        src_bin = real_bazel_env.bazel_bins[only_ver]
        dst_bin = bin_dir / f"bazel{exe_suffix}"
        try:
            dst_bin.symlink_to(src_bin)
        except OSError:
            shutil.copy2(src_bin, dst_bin)
    elif installed_kind == _InstalledKind.WRAPPER:
        _install_bazel_wrapper(
            bin_dir,
            {v: real_bazel_env.bazel_bins[v] for v in installed_versions},
            default_version=installed_versions[-1],
        )
    else:
        raise ValueError(f"Unsupported installed_kind: {installed_kind!r}")

    monkeypatch.chdir(ws)
    monkeypatch.delenv("BAZEL_BIN", raising=False)
    monkeypatch.delenv("USE_BAZEL_VERSION", raising=False)
    monkeypatch.setenv(
        "PATH",
        f"{bin_dir}{os.pathsep}{real_bazel_env.clean_system_path}",
    )

    if expect_fallback_to_bazelisk:
        # Point BAZELISK_HOME to the version-specific cache so bazelisk does not
        # have to re-download the Bazel binary from the network.
        monkeypatch.setenv(
            "BAZELISK_HOME",
            str(real_bazel_env.bazelisk_home / f"v_{required_version}"),
        )
    else:
        # Prove that bazel517 used the installed `bazel` on PATH and did NOT fall
        # back to downloading bazelisk.
        monkeypatch.setenv("BAZEL517_CACHE_DIR", str(tmp_path / "empty_cache"))
        monkeypatch.setenv(
            "BAZEL517_BAZELISK_BASE_URL",
            "http://127.0.0.1:1/unreachable",
        )

    # 1. Build and verify standard wheel with real Bazel
    whl_out = tmp_path / "whl_out"
    whl_name = bazel517.build_wheel(str(whl_out))
    _verify_built_wheel(
        whl_out / whl_name,
        tmp_path / "site_std",
        expected_bazel_version=required_version,
        expected_bazelrc_tag=tag,
    )

    # 2. Build and verify PEP 660 editable wheel with real Bazel (`build` + `aquery`)
    edit_out = tmp_path / "edit_out"
    edit_name = bazel517.build_editable(str(edit_out))
    site_edit = tmp_path / "site_edit"
    _verify_built_wheel(
        edit_out / edit_name,
        site_edit,
        expected_bazel_version=required_version,
        expected_bazelrc_tag=f"{tag}_editable",
    )

    # Verify live workspace source edits take effect without rebuilding
    (ws / "src" / "sample_pkg" / "__init__.py").write_text(
        textwrap.dedent(
            """\
            from ._build_info import BAZEL_VERSION, BAZELRC_TAG

            LIVE_VALUE = "edited_live"
            """
        ),
        encoding="utf-8",
    )
    live_script = textwrap.dedent(
        f"""\
        import site

        site.addsitedir({str(site_edit)!r})
        import sample_pkg

        assert sample_pkg.LIVE_VALUE == "edited_live"
        assert sample_pkg.BAZEL_VERSION == {required_version!r}
        assert sample_pkg.BAZELRC_TAG == {f"{tag}_editable"!r}
        """
    )
    live_check = subprocess.run(
        [sys.executable, "-I", "-c", live_script],
        check=True,
        capture_output=True,
        text=True,
    )
    assert live_check.returncode == 0
