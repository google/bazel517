from __future__ import annotations

from collections.abc import Callable, Mapping
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


def test_get_requires_hooks() -> None:
    assert bazel517.get_requires_for_build_wheel({"opt": "1"}) == []
    assert bazel517.get_requires_for_build_editable(None) == []
    assert bazel517.get_requires_for_build_sdist() == []


def test_build_wheel_and_editable_as_installed(
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

    # Build editable wheel and unpack into an isolated site-packages directory
    edit_out = tmp_path / "editable"
    edit_name = bazel517.build_editable(str(edit_out))
    edit_path = edit_out / edit_name
    site_edit = tmp_path / "site_edit"
    with zipfile.ZipFile(edit_path) as zf:
        names = set(zf.namelist())
        assert "_editable_mypkg.pth" in names
        assert "_editable_mypkg.py" in names
        assert "mypkg/_generated.py" in names
        assert "mypkg/_ext.so" in names
        assert "mypkg/data.json" in names
        assert "mypkg/__init__.py" not in names
        zf.extractall(site_edit)

    check_script = (
        f"import importlib.util, json, pathlib, site; "
        f"site.addsitedir({str(site_edit)!r}); "
        "import _editable_mypkg; _editable_mypkg.install(); "
        "import mypkg, mypkg._generated; "
        "ext_spec = importlib.util.find_spec('mypkg._ext'); "
        "assert ext_spec is not None and ext_spec.origin is not None; "
        "print(json.dumps({"
        "'x': mypkg.X, "
        "'init': str(pathlib.Path(mypkg.__file__).resolve()), "
        "'gen': mypkg._generated.GENERATED, "
        "'gen_file': str(pathlib.Path(mypkg._generated.__file__).resolve()), "
        "'ext_origin': str(pathlib.Path(ext_spec.origin).resolve())"
        "}))"
    )
    res1 = subprocess.run(
        [sys.executable, "-I", "-c", check_script],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    info1 = json.loads(res1.stdout.strip())
    assert info1["x"] == 42
    assert info1["init"] == str(
        (project_dir / "src" / "mypkg" / "__init__.py").resolve()
    )
    assert info1["gen"] is True
    assert info1["gen_file"] == str((site_edit / "mypkg" / "_generated.py").resolve())
    expected_ext = "_ext.pyd" if sys.platform == "win32" else "_ext.so"
    assert info1["ext_origin"] == str((site_edit / "mypkg" / expected_ext).resolve())

    # Edit workspace source file without rebuilding and verify live change + extension
    (project_dir / "src" / "mypkg" / "__init__.py").write_text(
        "from . import _generated\nX = 99\n",
        encoding="utf-8",
    )
    res2 = subprocess.run(
        [sys.executable, "-I", "-c", check_script],
        cwd=tmp_path,
        check=True,
        capture_output=True,
        text=True,
    )
    info2 = json.loads(res2.stdout.strip())
    assert info2["x"] == 99
    assert info2["gen"] is True
    assert info2["ext_origin"] == str((site_edit / "mypkg" / expected_ext).resolve())


def test_build_editable_bep_canonical_flags_and_edge_cases(
    project_dir: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bazel_factory: Callable[..., pathlib.Path],
) -> None:
    monkeypatch.chdir(project_dir)
    abs_inputs = (
        project_dir
        / "bazel-out"
        / "k8-opt"
        / "bin"
        / "ci"
        / "wheel"
        / "mypkg_wheel_target_wrapped_inputs.txt"
    )
    canonical_event = {
        "id": {"structuredCommandLine": {"commandLineLabel": "canonical"}},
        "structuredCommandLine": {
            "commandLineLabel": "canonical",
            "sections": [
                {"sectionLabel": "executable", "chunkList": {"chunk": ["bazel"]}},
                {
                    "sectionLabel": "command options",
                    "optionList": {
                        "option": [
                            {
                                "combinedForm": "--client_env=FOO=BAR",
                                "optionName": "client_env",
                                "metadataTags": ["HIDDEN"],
                                "source": "options generated by bazel launcher",
                            },
                            {
                                "combinedForm": "--flag_alias=x=y",
                                "optionName": "flag_alias",
                                "metadataTags": ["NON_CONFIGURABLE"],
                                "source": "module resolution",
                            },
                            {
                                "combinedForm": "--config=wheel_common",
                                "optionName": "config",
                                "source": "command line options",
                            },
                            {
                                "combinedForm": "--build_event_json_file=/tmp/bep.json",
                                "optionName": "build_event_json_file",
                                "source": "command line options",
                            },
                            {
                                "combinedForm": "--cxxopt=-std=c++17",
                                "optionName": "cxxopt",
                                "source": "/path/to/.bazelrc",
                            },
                            {
                                "combinedForm": "-c opt",
                                "optionName": "compilation_mode",
                                "source": "command line options",
                            },
                            {
                                "combinedForm": "--define=from_cfg=1",
                                "optionName": "define",
                                "source": "expanded from --config=wheel_common",
                            },
                            {
                                "combinedForm": "--//:wheel_build=true",
                                "optionName": "//:wheel_build",
                            },
                        ]
                    },
                },
            ],
        },
    }
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

    log_file = tmp_path / "bazel_invocations.jsonl"
    if log_file.exists():
        log_file.unlink()

    fake_bazel_factory(
        tmp_path,
        project_dir,
        bep_lines=["", json.dumps(canonical_event), json.dumps(files_event)],
        include_dir_entry=True,
        include_record=True,
        aquery_actions=[
            {"mnemonic": "PyWheel", "arguments": []},
            {
                "mnemonic": "PyWheel",
                "arguments": [
                    "wheelmaker",
                    "--strip_path_prefix=src",
                    "--input_file_list",
                    str(abs_inputs),
                    "--input_file",
                    "root_mod.py;root_mod.py",
                    "--input_file",
                    "src/mypkg/data.json;src/mypkg/data.json",
                    "--input_file",
                    "invalid_entry_without_semicolon",
                    "--input_file",
                    "external/foo/bar.py;external/foo/bar.py",
                    "--input_file",
                    "src/mypkg/missing.py;src/mypkg/missing.py",
                    "--input_file",
                    "completely/different.py;src/mypkg/__init__.py",
                ],
            },
        ],
        extra_input_lines=["   "],
    )

    edit_out = tmp_path / "editable_edge"
    edit_name = bazel517.build_editable(str(edit_out))
    with zipfile.ZipFile(edit_out / edit_name) as zf:
        names = set(zf.namelist())
        assert "mypkg-0.1.0.dist-info/RECORD" in names
        pth_lines = zf.read("_editable_mypkg.pth").decode("utf-8").splitlines()
        assert pth_lines == [
            str(project_dir.resolve()),
            str((project_dir / "src").resolve()),
            "import _editable_mypkg; _editable_mypkg.install()",
        ]

    invocations = [
        json.loads(line)
        for line in log_file.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]
    assert [inv["cmd"] for inv in invocations] == ["build", "aquery"]
    aquery_args = invocations[1]["args"]
    assert "--config=editable" not in aquery_args
    assert "--define=from_cfg=1" in aquery_args
    assert "--//:wheel_build=true" in aquery_args


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
        (
            {"include_record": False},
            "is missing a .dist-info/RECORD entry",
        ),
        (
            {"aquery_actions": []},
            "No PyWheel action found on target '//ci/wheel:mypkg_wheel'",
        ),
        (
            {"write_inputs_txt": False},
            "PyWheel input_file_list .* does not exist on disk",
        ),
    ],
)
def test_bazel_error_conditions(
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
        bazel517.build_editable(str(tmp_path / "out"))


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
