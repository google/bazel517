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

from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
import hashlib
import json
import pathlib
import platform
import shutil
import subprocess
import sys
from typing import Any
import zipfile

import pytest

import bazel517


def test_get_requires_for_build_wheel() -> None:
    assert bazel517.get_requires_for_build_wheel() == []
    assert bazel517.get_requires_for_build_wheel({"opt": "1"}) == []


def test_build_wheel_as_installed(
    project_dir: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(project_dir)
    monkeypatch.setenv("BAZEL517_BAZEL_ARGS", "--jobs=4")

    wheel_out = tmp_path / "wheels"
    whl_name = bazel517.build_wheel(
        str(wheel_out),
        config_settings={
            "bazel-args": "--keep_going",
            "bazel_args": ["--verbose_failures", "--subcommands"],
            "unrelated": 123,
        },
    )
    assert whl_name == "mypkg-0.1.0-cp313-cp313-linux_x86_64.whl"
    whl_path = wheel_out / whl_name
    assert whl_path.is_file()

    # Unpack standard wheel and verify import in an isolated subprocess
    site_std = tmp_path / "site_std"
    with zipfile.ZipFile(whl_path) as zf:
        names = set(zf.namelist())
        assert "mypkg/__init__.py" in names
        assert "mypkg/_generated.py" in names
        assert "mypkg/_ext.so" in names
        assert "mypkg-0.1.0.dist-info/METADATA" in names
        zf.extractall(site_std)

    std_check = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                f"import site; site.addsitedir({str(site_std)!r}); "
                "import mypkg, mypkg._generated; "
                "assert mypkg.X == 42; "
                "assert mypkg._generated.GENERATED is True"
            ),
        ],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    assert std_check.returncode == 0


def test_build_wheel_bep_file_entries(
    project_dir: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bazel_factory: Callable[..., pathlib.Path],
) -> None:
    monkeypatch.chdir(project_dir)
    abs_whl = (
        project_dir
        / "bazel-out"
        / "k8-opt"
        / "bin"
        / "ci"
        / "wheel"
        / "mypkg-0.1.0-cp313-cp313-linux_x86_64.whl"
    )
    files_event = {
        "id": {"namedSet": {"id": "0"}},
        "namedSetOfFiles": {
            "files": [
                {},
                {
                    "name": "other.txt",
                    "pathPrefix": ["bazel-out", "k8-opt", "bin", "ci", "wheel"],
                },
                {
                    "name": "mypkg-0.1.0-cp313-cp313-linux_x86_64.whl",
                    "pathPrefix": ["bazel-out", "k8-opt", "bin", "ci", "wheel"],
                },
                {
                    "name": str(abs_whl),
                },
            ]
        },
    }
    fake_bazel_factory(
        tmp_path,
        project_dir,
        bep_lines=["", json.dumps(files_event)],
    )
    wheel_out = tmp_path / "wheels_bep"
    whl_name = bazel517.build_wheel(str(wheel_out))
    assert (wheel_out / whl_name).is_file()


@pytest.mark.parametrize(
    ("setup_kwargs", "expected_match"),
    [
        (
            {"bep_lines": []},
            "Expected Bazel target '//ci/wheel:mypkg_wheel' to produce",
        ),
        (
            {"create_wheel_file": False},
            "Bazel reported wheel output at .* but the file does not exist",
        ),
    ],
)
def test_build_wheel_error_conditions(
    project_dir: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bazel_factory: Callable[..., pathlib.Path],
    setup_kwargs: Mapping[str, Any],
    expected_match: str,
) -> None:
    monkeypatch.chdir(project_dir)
    fake_bazel_factory(tmp_path, project_dir, **setup_kwargs)
    with pytest.raises(bazel517.BazelError, match=expected_match):
        bazel517.build_wheel(str(tmp_path / "out"))


def test_bazel_bin_path_resolution_and_bazelisk_fallback_download(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bazel_factory: Callable[..., pathlib.Path],
) -> None:
    proj = tmp_path / "proj_dl"
    pkg = proj / "src" / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    (proj / "pyproject.toml").write_text(
        """
[build-system]
requires = ["bazel517"]
build-backend = "bazel517"

[tool.bazel517]
target = "//ci/wheel:mypkg_wheel"
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(proj)
    monkeypatch.delenv("BAZEL_BIN", raising=False)
    monkeypatch.delenv("USE_BAZEL_VERSION", raising=False)

    fake_script = fake_bazel_factory(tmp_path, proj)
    log_file = tmp_path / "bazel_invocations.jsonl"

    # 1. When both `bazelisk` and `bazel` are on PATH, `bazelisk` is preferred.
    path_dir = tmp_path / "bin_on_path"
    path_dir.mkdir()
    exe_suffix = ".exe" if sys.platform == "win32" else ""
    shutil.copy2(fake_script, path_dir / f"bazelisk{exe_suffix}")
    shutil.copy2(fake_script, path_dir / f"bazel{exe_suffix}")
    monkeypatch.setenv("PATH", str(path_dir))

    whl1 = bazel517.build_wheel(str(tmp_path / "w1"))
    assert (tmp_path / "w1" / whl1).is_file()

    # 2. When only `bazel` is on PATH, `bazel` is used.
    (path_dir / f"bazelisk{exe_suffix}").unlink()
    whl2 = bazel517.build_wheel(str(tmp_path / "w2"))
    assert (tmp_path / "w2" / whl2).is_file()

    # 3. When neither is on PATH, bazelisk is downloaded, verified, and cached.
    empty_path = tmp_path / "empty_path"
    empty_path.mkdir()
    monkeypatch.setenv("PATH", str(empty_path))

    mirror_dir = tmp_path / "mirror"
    mirror_dir.mkdir()
    asset_bytes = fake_script.read_bytes()
    asset_sha256 = hashlib.sha256(asset_bytes).hexdigest()
    for asset_name in (
        "bazelisk-linux-amd64",
        "bazelisk-linux-arm64",
        "bazelisk-darwin-amd64",
        "bazelisk-darwin-arm64",
        "bazelisk-windows-amd64.exe",
        "bazelisk-windows-arm64.exe",
    ):
        (mirror_dir / asset_name).write_bytes(asset_bytes)

    monkeypatch.setenv("BAZEL517_BAZELISK_BASE_URL", mirror_dir.as_uri())
    monkeypatch.setenv("BAZEL517_BAZELISK_SHA256", asset_sha256)

    # Test cache directory resolution via BAZEL517_CACHE_DIR, XDG_CACHE_HOME, and HOME
    cache_dir = tmp_path / "custom_cache"
    monkeypatch.setenv("BAZEL517_CACHE_DIR", str(cache_dir))
    whl3 = bazel517.build_wheel(str(tmp_path / "w3"))
    assert (tmp_path / "w3" / whl3).is_file()

    # Remove mirror files to prove the second invocation uses the cached binary
    shutil.rmtree(mirror_dir)
    whl4 = bazel517.build_wheel(str(tmp_path / "w4"))
    assert (tmp_path / "w4" / whl4).is_file()

    # Test XDG_CACHE_HOME and HOME cache dir branches + download failure
    monkeypatch.delenv("BAZEL517_CACHE_DIR")
    monkeypatch.setenv("XDG_CACHE_HOME", str(tmp_path / "xdg_cache"))
    with pytest.raises(bazel517.BazelError, match="Failed to download bazelisk"):
        bazel517.build_wheel(str(tmp_path / "w_fail_dl"))

    monkeypatch.delenv("XDG_CACHE_HOME")
    monkeypatch.setenv("HOME", str(tmp_path / "fake_home"))
    monkeypatch.setenv("USERPROFILE", str(tmp_path / "fake_home"))
    mirror_dir.mkdir()
    for asset_name in (
        "bazelisk-linux-amd64",
        "bazelisk-linux-arm64",
        "bazelisk-darwin-amd64",
        "bazelisk-darwin-arm64",
        "bazelisk-windows-amd64.exe",
        "bazelisk-windows-arm64.exe",
    ):
        (mirror_dir / asset_name).write_bytes(asset_bytes)

    # Test SHA-256 mismatch error
    monkeypatch.setenv("BAZEL517_BAZELISK_SHA256", "0" * 64)
    with pytest.raises(bazel517.BazelError, match="SHA-256 mismatch for bazelisk"):
        bazel517.build_wheel(str(tmp_path / "w_fail_sha"))

    # Test unsupported platform/architecture error
    monkeypatch.setattr(platform, "machine", lambda: "unsupported_arch")
    with pytest.raises(
        bazel517.BazelError,
        match="no prebuilt bazelisk binary is available",
    ):
        bazel517.build_wheel(str(tmp_path / "w_fail_arch"))
    assert log_file.is_file()


@pytest.mark.parametrize(
    (
        "bazelversion_content",
        "bazeliskrc_content",
        "use_bazel_version",
        "bazelrc_content",
        "path_bin_name",
        "path_bazel_version",
        "path_installed_versions",
        "expected_active_version",
        "expected_bazelrc_flags",
    ),
    [
        # 1. Exact match between `.bazelversion` (8.6.0) and standalone `bazel` on PATH
        (
            "# Workspace Bazel pin\n\n8.6.0\n",
            None,
            None,
            "# Build flags for Bazel 8\nbuild --require_bazel_major=8\n",
            "bazel",
            "8.6.0",
            None,
            "8.6.0",
            ["build --require_bazel_major=8"],
        ),
        # 2. `.bazelversion` with vendor suffix on installed `bazel` (8.6.0-homebrew)
        (
            "8.6.0\n",
            None,
            None,
            "build --require_bazel_major=8 --define=homebrew_bazel=1\n",
            "bazel",
            "8.6.0-homebrew",
            None,
            "8.6.0-homebrew",
            ["build --require_bazel_major=8 --define=homebrew_bazel=1"],
        ),
        # 3. Floating minor range `8.x` satisfied by installed `bazel` 8.6.0
        (
            "8.x\n",
            None,
            None,
            "build --require_bazel_major=8 --enable_bzlmod\n",
            "bazel",
            "8.6.0",
            None,
            "8.6.0",
            ["build --require_bazel_major=8 --enable_bzlmod"],
        ),
        # 4. Version mismatch: `.bazelversion` requires 8.6.0, but standalone `bazel`
        # on PATH is 7.4.1 -> skips PATH `bazel` and falls back to bazelisk (8.6.0)
        (
            "8.6.0\n",
            None,
            None,
            "build --require_bazel_major=8 --incompatible_autoload_externally=\n",
            "bazel",
            "7.4.1",
            None,
            "8.6.0",
            ["build --require_bazel_major=8 --incompatible_autoload_externally="],
        ),
        # 5. Floating range mismatch: `.bazelversion` requires `8.x`, but standalone
        # `bazel` on PATH is 7.4.1 -> falls back to bazelisk (8.0)
        (
            "8.x\n",
            None,
            None,
            "build --require_bazel_major=8 --define=series=8\n",
            "bazel",
            "7.4.1",
            None,
            "8.0",
            ["build --require_bazel_major=8 --define=series=8"],
        ),
        # 6. Multi-version `bazel` wrapper on PATH with 7.4.1 and 8.6.0 installed:
        # project requires 7.4.1 -> wrapper dispatches to 7.4.1
        (
            "7.4.1\n",
            None,
            None,
            "build --require_bazel_major=7 --noenable_bzlmod\n",
            "bazel",
            "8.6.0",
            ("7.4.1", "8.6.0"),
            "7.4.1",
            ["build --require_bazel_major=7 --noenable_bzlmod"],
        ),
        # 7. Multi-version `bazel` wrapper on PATH with 7.4.1 and 8.6.0 installed:
        # project requires 9.0.0 (not installed) -> wrapper exits non-zero on
        # `--version`, so bazel517 falls back to downloaded bazelisk (9.0.0)
        (
            "9.0.0\n",
            None,
            None,
            "build --require_bazel_major=9\n",
            "bazel",
            "8.6.0",
            ("7.4.1", "8.6.0"),
            "9.0.0",
            ["build --require_bazel_major=9"],
        ),
        # 8. `.bazeliskrc` specifies `USE_BAZEL_VERSION=7.4.1` while standalone `bazel`
        # on PATH is 8.6.0 -> falls back to bazelisk (7.4.1)
        (
            None,
            "# Bazelisk configuration\nBAZELISK_SHUTDOWN=1\nUSE_BAZEL_VERSION=7.4.1\n",
            None,
            "build --require_bazel_major=7 --define=from_bazeliskrc=1\n",
            "bazel",
            "8.6.0",
            None,
            "7.4.1",
            ["build --require_bazel_major=7 --define=from_bazeliskrc=1"],
        ),
        # 9. `.bazeliskrc` without `USE_BAZEL_VERSION` falls through to `.bazelversion`
        (
            "8.6.0\n",
            "# Only non-version settings\nBAZELISK_SHUTDOWN=1\n",
            None,
            "build --require_bazel_major=8\n",
            "bazel",
            "8.6.0",
            None,
            "8.6.0",
            ["build --require_bazel_major=8"],
        ),
        # 10. `USE_BAZEL_VERSION` overrides `.bazelversion` when standalone `bazel` on
        # PATH is 8.6.0 -> falls back to bazelisk to run 7.4.1
        (
            "8.6.0\n",
            None,
            "7.4.1",
            "build --require_bazel_major=7 --define=from_env_version=7.4.1\n",
            "bazel",
            "8.6.0",
            None,
            "7.4.1",
            ["build --require_bazel_major=7 --define=from_env_version=7.4.1"],
        ),
        # 11. `USE_BAZEL_VERSION=latest` accepts installed `bazel` 7.4.1 on PATH
        (
            "8.6.0\n",
            None,
            "latest",
            "build --require_bazel_major=7 --define=latest_ok=1\n",
            "bazel",
            "7.4.1",
            None,
            "7.4.1",
            ["build --require_bazel_major=7 --define=latest_ok=1"],
        ),
        # 12. `.bazelversion` set to `latest` accepts installed `bazel` 7.4.1 on PATH
        (
            "latest\n",
            None,
            None,
            "build --require_bazel_major=7 --define=bv_latest=1\n",
            "bazel",
            "7.4.1",
            None,
            "7.4.1",
            ["build --require_bazel_major=7 --define=bv_latest=1"],
        ),
        # 13. `.bazelversion` with only comments/blank lines accepts installed `bazel`
        (
            "# comment only\n   \n",
            None,
            None,
            "build --require_bazel_major=7 --define=unconstrained=1\n",
            "bazel",
            "7.4.1",
            None,
            "7.4.1",
            ["build --require_bazel_major=7 --define=unconstrained=1"],
        ),
        # 14. Broken `bazel` on PATH whose `--version` output is malformed when a
        # specific version is required -> falls back to bazelisk
        (
            "8.6.0\n",
            None,
            None,
            "build --require_bazel_major=8 --define=fallback_from_malformed=1\n",
            "bazel",
            "malformed",
            None,
            "8.6.0",
            ["build --require_bazel_major=8 --define=fallback_from_malformed=1"],
        ),
        # 15. `bazelisk` on PATH handles any required version directly without fallback
        (
            "7.4.1\n",
            None,
            None,
            "build --require_bazel_major=7 --define=via_path_bazelisk=1\n",
            "bazelisk",
            "8.6.0",
            ("*",),
            "7.4.1",
            ["build --require_bazel_major=7 --define=via_path_bazelisk=1"],
        ),
    ],
)
def test_resolve_bazel_bin_version_matching_unit(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bazel_factory: Callable[..., pathlib.Path],
    bazelversion_content: str | None,
    bazeliskrc_content: str | None,
    use_bazel_version: str | None,
    bazelrc_content: str | None,
    path_bin_name: str,
    path_bazel_version: str,
    path_installed_versions: Sequence[str] | None,
    expected_active_version: str,
    expected_bazelrc_flags: list[str],
) -> None:
    proj = tmp_path / "proj_ver"
    pkg = proj / "src" / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("X = 42\n", encoding="utf-8")
    (proj / "pyproject.toml").write_text(
        """
[build-system]
requires = ["bazel517"]
build-backend = "bazel517"

[project]
name = "mypkg"
version = "0.1.0"

[tool.bazel517]
target = "//ci/wheel:mypkg_wheel"
bazel-args = ["-c", "opt"]
""",
        encoding="utf-8",
    )
    if bazelversion_content is not None:
        (proj / ".bazelversion").write_text(bazelversion_content, encoding="utf-8")
    if bazeliskrc_content is not None:
        (proj / ".bazeliskrc").write_text(bazeliskrc_content, encoding="utf-8")
    if bazelrc_content is not None:
        (proj / ".bazelrc").write_text(bazelrc_content, encoding="utf-8")

    monkeypatch.chdir(proj)
    monkeypatch.delenv("BAZEL_BIN", raising=False)
    if use_bazel_version is not None:
        monkeypatch.setenv("USE_BAZEL_VERSION", use_bazel_version)
    else:
        monkeypatch.delenv("USE_BAZEL_VERSION", raising=False)

    log_dir = tmp_path / "logs"
    log_dir.mkdir()
    log_file = log_dir / "bazel_invocations.jsonl"

    exe_suffix = ".exe" if sys.platform == "win32" else ""
    path_dir = tmp_path / "path_bin"
    path_dir.mkdir()
    path_script = fake_bazel_factory(
        tmp_path / "path_build",
        proj,
        bazel_version=path_bazel_version,
        installed_versions=path_installed_versions,
        log_dir=log_dir,
    )
    shutil.copy2(path_script, path_dir / f"{path_bin_name}{exe_suffix}")
    monkeypatch.setenv("PATH", str(path_dir))

    # Set up fallback bazelisk mirror capable of running any requested Bazel version
    fallback_script = fake_bazel_factory(
        tmp_path / "fallback_build",
        proj,
        bazel_version="8.6.0",
        installed_versions=("*",),
        log_dir=log_dir,
    )
    mirror_dir = tmp_path / "mirror"
    mirror_dir.mkdir()
    asset_bytes = fallback_script.read_bytes()
    asset_sha256 = hashlib.sha256(asset_bytes).hexdigest()
    for asset_name in (
        "bazelisk-linux-amd64",
        "bazelisk-linux-arm64",
        "bazelisk-darwin-amd64",
        "bazelisk-darwin-arm64",
        "bazelisk-windows-amd64.exe",
        "bazelisk-windows-arm64.exe",
    ):
        (mirror_dir / asset_name).write_bytes(asset_bytes)

    monkeypatch.setenv("BAZEL517_BAZELISK_BASE_URL", mirror_dir.as_uri())
    monkeypatch.setenv("BAZEL517_BAZELISK_SHA256", asset_sha256)
    monkeypatch.setenv("BAZEL517_CACHE_DIR", str(tmp_path / "bazelisk_cache"))

    wheel_out = tmp_path / "wheel_out"
    whl_name = bazel517.build_wheel(str(wheel_out))
    assert (wheel_out / whl_name).is_file()

    invocations = [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert len(invocations) == 1
    assert invocations[0]["cmd"] == "build"
    assert invocations[0]["bazel_version"] == expected_active_version
    assert invocations[0]["bazelrc_flags"] == expected_bazelrc_flags
