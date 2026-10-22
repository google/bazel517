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
import json
import os
import pathlib
import shutil
import stat
import subprocess
import sys
import zipfile

import pytest

import bazel517
from bazel517 import _bazel

_BAZEL_VERSIONS: tuple[str, str] = ("7.4.1", "8.6.0")


@dataclasses.dataclass(frozen=True, slots=True)
class RealBazelEnv:
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
        """\
def _bazel_version_repo_impl(rctx):
    rctx.file("BUILD.bazel", "")
    rctx.file("defs.bzl", "BAZEL_VERSION = %r\\n" % (native.bazel_version,))

bazel_version_repo = repository_rule(
    implementation = _bazel_version_repo_impl,
)

def _ext_impl(mctx):
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
""",
        encoding="utf-8",
    )
    (workspace_dir / "MODULE.bazel").write_text(
        """\
module(name = "sample_pkg", version = "0.1.0")
bazel_dep(name = "rules_python", version = "1.2.0")

bazel_version_ext = use_extension("//:build_info.bzl", "bazel_version_ext")
use_repo(bazel_version_ext, "bazel_version_info")
""",
        encoding="utf-8",
    )
    (workspace_dir / "BUILD.bazel").write_text(
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
""",
        encoding="utf-8",
    )
    pkg_dir = workspace_dir / "src" / "sample_pkg"
    pkg_dir.mkdir(parents=True, exist_ok=True)
    (pkg_dir / "__init__.py").write_text(
        """\
from ._build_info import BAZEL_VERSION, BAZELRC_TAG

LIVE_VALUE = "initial"
""",
        encoding="utf-8",
    )
    (workspace_dir / "pyproject.toml").write_text(
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
""",
        encoding="utf-8",
    )


def _install_bazel_wrapper(
    bin_dir: pathlib.Path,
    installed_bins: Mapping[str, pathlib.Path],
    default_version: str,
) -> None:
    """Install a multi-version `bazel` wrapper script into `bin_dir`."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    bins_dict = {ver: str(path) for ver, path in installed_bins.items()}
    wrapper_code = f"""#!{sys.executable}
import os
import pathlib
import subprocess
import sys

installed = {bins_dict!r}
default_ver = {default_version!r}

req = os.environ.get("USE_BAZEL_VERSION", "").strip()
if not req:
    cwd = pathlib.Path.cwd()
    markers = ("MODULE.bazel", "WORKSPACE", "WORKSPACE.bazel")
    for d in (cwd, *cwd.parents):
        if any((d / m).exists() for m in markers):
            bv = d / ".bazelversion"
            if bv.is_file():
                for raw in bv.read_text(encoding="utf-8").splitlines():
                    line = raw.strip()
                    if line and not line.startswith("#"):
                        req = line
                        break
            break

if not req or req == "latest":
    req = default_ver

target = installed.get(req)
if target is None:
    sys.stderr.write(
        f"ERROR: The project you're trying to build requires Bazel {{req}}, "
        f"but only {{sorted(installed)}} are installed.\\n"
    )
    sys.exit(1)

sys.exit(subprocess.call([target, *sys.argv[1:]]))
"""
    if sys.platform == "win32":
        script_py = bin_dir / "_bazel_wrapper_impl.py"
        script_py.write_text(wrapper_code, encoding="utf-8")
        wrapper_exe = bin_dir / "bazel.exe"
        cs_path = bin_dir / "_bazel_wrapper.cs"
        py_escaped = sys.executable.replace("\\", "\\\\")
        script_escaped = str(script_py).replace("\\", "\\\\")
        cs_path.write_text(
            "using System;\n"
            "using System.Diagnostics;\n"
            "class P {\n"
            "    static int Main() {\n"
            "        string cmd = Environment.CommandLine;\n"
            "        int i = 0;\n"
            '        if (cmd.StartsWith("\\"")) {\n'
            "            i = cmd.IndexOf('\"', 1) + 1;\n"
            "        } else {\n"
            "            while (i < cmd.Length && !char.IsWhiteSpace(cmd[i]))\n"
            "                i++;\n"
            "        }\n"
            "        string rest = cmd.Substring(i);\n"
            f'        string py = "{py_escaped}";\n'
            f'        string args = "\\"{script_escaped}\\"" + rest;\n'
            "        var psi = new ProcessStartInfo(py, args) {\n"
            "            UseShellExecute = false\n"
            "        };\n"
            "        var p = Process.Start(psi);\n"
            "        p.WaitForExit();\n"
            "        return p.ExitCode;\n"
            "    }\n"
            "}\n",
            encoding="utf-8",
        )
        csc = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
        subprocess.run(
            [csc, "/nologo", f"/out:{wrapper_exe}", str(cs_path)],
            check=True,
            capture_output=True,
        )
        return

    wrapper_path = bin_dir / "bazel"
    wrapper_path.write_text(wrapper_code, encoding="utf-8")
    wrapper_path.chmod(wrapper_path.stat().st_mode | stat.S_IEXEC)


@pytest.fixture(scope="session")
def real_bazel_env(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[RealBazelEnv]:
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
        yield RealBazelEnv(
            bazelisk_bin=bazelisk_bin,
            bazelisk_home=bazelisk_home,
            bazel_bins=bazel_bins,
            workspaces=workspaces,
            repo_cache=repo_cache,
            clean_system_path=clean_path,
        )
    finally:
        startup_args = ["--output_user_root=C:/b"] if sys.platform == "win32" else []
        for ver, ws in workspaces.items():
            if (ws / "MODULE.bazel").is_file():
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
        if rc_file.exists():
            rc_file.unlink()
    if bazelversion_text is not None:
        (ws / ".bazelversion").write_text(bazelversion_text, encoding="utf-8")
    if bazeliskrc_text is not None:
        (ws / ".bazeliskrc").write_text(bazeliskrc_text, encoding="utf-8")

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
        """\
from ._build_info import BAZEL_VERSION, BAZELRC_TAG

LIVE_VALUE = "initial"
""",
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
    check = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                f"import json, site; site.addsitedir({str(unpack_dir)!r}); "
                "import sample_pkg; "
                "print(json.dumps({"
                "'bazel_version': sample_pkg.BAZEL_VERSION, "
                "'bazelrc_tag': sample_pkg.BAZELRC_TAG, "
                "'live_value': sample_pkg.LIVE_VALUE"
                "}))"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    data = json.loads(check.stdout.strip())
    assert data["bazel_version"] == expected_bazel_version
    assert data["bazelrc_tag"] == expected_bazelrc_tag
    assert data["live_value"] == "initial"


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
        ("7.4.1", "bazelversion", "standalone", ("7.4.1",), False),
        # 2. Standalone `bazel` 7.4.1 on PATH does NOT match `.bazelversion` 8.6.0
        # -> skips PATH `bazel` 7.4.1 and falls back to bazelisk to run real Bazel 8.6.0
        ("8.6.0", "bazelversion", "standalone", ("7.4.1",), True),
        # 3. Standalone `bazel` 8.6.0 on PATH satisfies floating `.bazelversion` `8.x`
        ("8.6.0", "bazelversion_floating", "standalone", ("8.6.0",), False),
        # 4. Standalone `bazel` 8.6.0 on PATH does NOT match `.bazeliskrc` (7.4.1)
        # -> falls back to bazelisk to run real Bazel 7.4.1
        ("7.4.1", "bazeliskrc", "standalone", ("8.6.0",), True),
        # 5. Multi-version `bazel` wrapper on PATH with both 7.4.1 and 8.6.0 installed
        # dispatches directly to 7.4.1 when `.bazelversion` requires 7.4.1
        ("7.4.1", "bazelversion", "wrapper", ("7.4.1", "8.6.0"), False),
        # 6. Multi-version `bazel` wrapper on PATH with both 7.4.1 and 8.6.0 installed
        # dispatches directly to 8.6.0 when `.bazelversion` requires 8.6.0
        ("8.6.0", "bazelversion", "wrapper", ("7.4.1", "8.6.0"), False),
        # 7. Multi-version `bazel` wrapper on PATH with only 7.4.1 installed fails on
        # `.bazelversion` 8.6.0 -> falls back to bazelisk to run real Bazel 8.6.0
        ("8.6.0", "bazelversion", "wrapper", ("7.4.1",), True),
    ],
)
def test_real_bazel_wheel_and_editable_across_versions(
    real_bazel_env: RealBazelEnv,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    required_version: str,
    version_source: str,
    installed_kind: str,
    installed_versions: Sequence[str],
    expect_fallback_to_bazelisk: bool,
) -> None:
    ws = real_bazel_env.workspaces[required_version]
    major_minor = ".".join(required_version.split(".")[:2])
    tag = f"tag_{required_version.replace('.', '_')}_{installed_kind}"

    if version_source == "bazelversion":
        bv_text: str | None = f"{required_version}\n"
        brc_text: str | None = None
    elif version_source == "bazelversion_floating":
        major = required_version.split(".")[0]
        bv_text = f"{major}.x\n"
        brc_text = None
    else:
        assert version_source == "bazeliskrc"
        bv_text = None
        brc_text = f"USE_BAZEL_VERSION={required_version}\n"

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
    if installed_kind == "standalone":
        only_ver = installed_versions[0]
        src_bin = real_bazel_env.bazel_bins[only_ver]
        dst_bin = bin_dir / f"bazel{exe_suffix}"
        try:
            dst_bin.symlink_to(src_bin)
        except OSError:
            shutil.copy2(src_bin, dst_bin)
    else:
        assert installed_kind == "wrapper"
        _install_bazel_wrapper(
            bin_dir,
            {v: real_bazel_env.bazel_bins[v] for v in installed_versions},
            default_version=installed_versions[-1],
        )

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
        """\
from ._build_info import BAZEL_VERSION, BAZELRC_TAG

LIVE_VALUE = "edited_live"
""",
        encoding="utf-8",
    )
    live_check = subprocess.run(
        [
            sys.executable,
            "-I",
            "-c",
            (
                f"import site; site.addsitedir({str(site_edit)!r}); "
                "import sample_pkg; "
                "assert sample_pkg.LIVE_VALUE == 'edited_live'; "
                f"assert sample_pkg.BAZEL_VERSION == {required_version!r}; "
                f"assert sample_pkg.BAZELRC_TAG == {f'{tag}_editable'!r}"
            ),
        ],
        check=True,
        capture_output=True,
        text=True,
    )
    assert live_check.returncode == 0
