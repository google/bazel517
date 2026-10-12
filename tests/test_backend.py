from __future__ import annotations

from collections.abc import Mapping, Sequence
import pathlib
import stat
import subprocess
import tarfile
from typing import Any
import zipfile

import hypothesis
import hypothesis.strategies as st
import pytest

import bazel517


def _write_fake_bazel(
    bin_dir: pathlib.Path,
    workspace_dir: pathlib.Path,
    *,
    cquery_outputs: Sequence[str] | None = None,
    create_wheel_file: bool = True,
    include_dir_entry: bool = False,
    include_record: bool = True,
    aquery_actions: Sequence[Mapping[str, Any]] | None = None,
    write_inputs_txt: bool = True,
    extra_input_lines: Sequence[str] = (),
) -> pathlib.Path:
    """Create an executable script simulating `bazel build`, `cquery`, and `aquery`."""
    fake_bazel = bin_dir / "fake_bazel"
    wheel_bin_dir = workspace_dir / "bazel-out" / "k8-opt" / "bin" / "ci" / "wheel"
    out_whl = wheel_bin_dir / "mypkg-0.1.0-cp313-cp313-linux_x86_64.whl"
    inputs_txt = wheel_bin_dir / "mypkg_wheel_target_wrapped_inputs.txt"

    default_cquery = [out_whl.relative_to(workspace_dir).as_posix()]
    cquery_lines = (
        list(cquery_outputs) if cquery_outputs is not None else default_cquery
    )

    default_actions: list[Mapping[str, Any]] = [
        {
            "mnemonic": "PyWheel",
            "arguments": [
                "wheelmaker",
                "--strip_path_prefix=ci/wheel/pkg_stage",
                "--strip_path_prefix=src",
                "--input_file_list",
                inputs_txt.relative_to(workspace_dir).as_posix(),
            ],
        }
    ]
    actions_list = (
        list(aquery_actions) if aquery_actions is not None else default_actions
    )

    input_lines = [
        "src/mypkg/__init__.py;src/mypkg/__init__.py",
        "ci/wheel/pkg_stage/mypkg/_ext.so;"
        "bazel-out/k8-opt/bin/ci/wheel/pkg_stage/mypkg/_ext.so",
        *extra_input_lines,
    ]

    script = f"""#!/usr/bin/env python3
import json
import os
import pathlib
import sys
import zipfile

args = sys.argv[1:]
cmd = args[0]
out_whl = pathlib.Path({str(out_whl)!r})
inputs_txt = pathlib.Path({str(inputs_txt)!r})
log_file = pathlib.Path({str(bin_dir / "bazel_invocations.jsonl")!r})

with log_file.open("a", encoding="utf-8") as lf:
    lf.write(
        json.dumps(
            {{
                "cmd": cmd,
                "args": args,
                "custom_env": os.environ.get("CUSTOM_BAZEL_VAR"),
            }}
        )
        + "\\n"
    )

if cmd == "build":
    out_whl.parent.mkdir(parents=True, exist_ok=True)
    if {write_inputs_txt!r}:
        inputs_txt.write_text(
            {chr(10).join(input_lines)!r} + "\\n",
            encoding="utf-8",
        )
    if {create_wheel_file!r}:
        with zipfile.ZipFile(out_whl, "w") as zf:
            if {include_dir_entry!r}:
                zf.writestr("mypkg/", "")
            zf.writestr("mypkg/__init__.py", "X = 42\\n")
            zf.writestr("mypkg/data.json", "{{}}\\n")
            zf.writestr("mypkg/_ext.so", b"\\x7fELF-fake-shared-object")
            zf.writestr(
                "mypkg-0.1.0.dist-info/METADATA",
                "Metadata-Version: 2.4\\nName: mypkg\\nVersion: 0.1.0\\n",
            )
            zf.writestr(
                "mypkg-0.1.0.dist-info/WHEEL",
                "Wheel-Version: 1.0\\nTag: cp313-cp313-linux_x86_64\\n",
            )
            if {include_record!r}:
                zf.writestr("mypkg-0.1.0.dist-info/RECORD", "")
elif cmd == "cquery":
    for line in {cquery_lines!r}:
        print(line)
elif cmd == "aquery":
    print(json.dumps({{"actions": {actions_list!r}}}))
else:
    sys.exit(2)
"""
    fake_bazel.write_text(script, encoding="utf-8")
    fake_bazel.chmod(fake_bazel.stat().st_mode | stat.S_IEXEC)
    return fake_bazel


@pytest.fixture
def project_dir(tmp_path: pathlib.Path) -> pathlib.Path:
    proj = tmp_path / "proj"
    pkg = proj / "src" / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("X = 42\n", encoding="utf-8")
    (pkg / "data.json").write_text("{}\n", encoding="utf-8")
    (proj / "root_mod.py").write_text("ROOT = True\n", encoding="utf-8")
    (proj / "LICENSE").write_text("Apache-2.0\n", encoding="utf-8")
    fake_bazel = _write_fake_bazel(tmp_path, proj)

    (proj / "pyproject.toml").write_text(
        f"""
[build-system]
requires = ["bazel517"]
build-backend = "bazel517"

[project]
name = "mypkg"
version = "0.1.0"
description = "Test package"
license = "Apache-2.0"
license-files = ["LICENSE"]
requires-python = ">=3.13"

[tool.bazel517]
bazel-bin = {str(fake_bazel)!r}
target = "//ci/wheel:mypkg_wheel"
bazel-args = ["-c", "opt", "--define=py={{python_version}}"]
editable-bazel-args = ["--config=editable", "--define=abi={{python_abi_tag}}"]

[tool.bazel517.env]
CUSTOM_BAZEL_VAR = "py-{{python_version_nodot}}-{{freethreaded}}"
""",
        encoding="utf-8",
    )
    return proj


def test_get_requires_hooks() -> None:
    assert bazel517.get_requires_for_build_wheel({"opt": "1"}) == []
    assert bazel517.get_requires_for_build_editable(None) == []
    assert bazel517.get_requires_for_build_sdist() == []


def test_build_wheel_and_editable(
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

    with zipfile.ZipFile(whl_path) as zf:
        names = set(zf.namelist())
        assert "mypkg/__init__.py" in names
        assert "mypkg/_ext.so" in names
        assert "mypkg-0.1.0.dist-info/METADATA" in names

    edit_out = tmp_path / "editable"
    edit_name = bazel517.build_editable(str(edit_out))
    edit_path = edit_out / edit_name
    with zipfile.ZipFile(edit_path) as zf:
        names = set(zf.namelist())
        assert "_editable_mypkg.pth" in names
        assert "mypkg/_ext.so" in names
        assert "mypkg/data.json" in names
        assert "mypkg/__init__.py" not in names
        pth_content = zf.read("_editable_mypkg.pth").decode().strip()
        assert pth_content == str((project_dir / "src").resolve())


def test_build_editable_edge_cases(
    project_dir: pathlib.Path,
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
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
    abs_inputs = (
        project_dir
        / "bazel-out"
        / "k8-opt"
        / "bin"
        / "ci"
        / "wheel"
        / "mypkg_wheel_target_wrapped_inputs.txt"
    )
    _write_fake_bazel(
        tmp_path,
        project_dir,
        cquery_outputs=["", str(abs_whl), "bazel-out/k8-opt/bin/ci/wheel/other.txt"],
        include_dir_entry=True,
        include_record=False,
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
        pth_lines = zf.read("_editable_mypkg.pth").decode().splitlines()
        assert pth_lines == [
            str(project_dir.resolve()),
            str((project_dir / "src").resolve()),
        ]


@pytest.mark.parametrize(
    ("setup_kwargs", "expected_match"),
    [
        (
            {"cquery_outputs": []},
            "Expected Bazel target '//ci/wheel:mypkg_wheel' to produce",
        ),
        (
            {"create_wheel_file": False},
            "Bazel reported wheel output at .* but the file does not exist",
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
    setup_kwargs: Mapping[str, Any],
    expected_match: str,
) -> None:
    monkeypatch.chdir(project_dir)
    _write_fake_bazel(tmp_path, project_dir, **setup_kwargs)
    with pytest.raises(bazel517.BazelError, match=expected_match):
        bazel517.build_editable(str(tmp_path / "out"))


@pytest.mark.parametrize(
    ("toml_content", "expected_exc", "expected_match"),
    [
        (None, FileNotFoundError, "pyproject.toml not found"),
        ("not_project = 1\n", ValueError, r"Missing \[project\] table"),
        (
            '[project]\nname = "a"\nversion = "1"\n\n'
            '[tool]\nbazel517 = "not-a-table"\n',
            ValueError,
            r"\[tool\.bazel517\] must be a TOML table",
        ),
        (
            '[project]\nversion = "1.0"\n',
            ValueError,
            r"Missing required \[project\]\.name",
        ),
        (
            '[project]\nname = "pkg"\n',
            ValueError,
            r"Missing required \[project\]\.version",
        ),
        (
            '[project]\nname = "pkg"\nversion = "1.0"\nreadme = { invalid = true }\n',
            ValueError,
            r"Unsupported \[project\]\.readme value",
        ),
        (
            '[project]\nname = "pkg"\nversion = "1.0"\nreadme = 123\n',
            ValueError,
            r"Unsupported \[project\]\.readme value",
        ),
        (
            '[project]\nname = "pkg"\nversion = "1.0"\n',
            ValueError,
            "Missing required Bazel wheel target",
        ),
    ],
)
def test_invalid_pyproject_configs(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    toml_content: str | None,
    expected_exc: type[Exception],
    expected_match: str,
) -> None:
    monkeypatch.chdir(tmp_path)
    if toml_content is not None:
        (tmp_path / "pyproject.toml").write_text(toml_content, encoding="utf-8")
    with pytest.raises(expected_exc, match=expected_match):
        bazel517.build_sdist(str(tmp_path / "sdist"))


@pytest.mark.parametrize(
    ("readme_toml", "license_toml", "people_toml", "expected_metadata_substrings"),
    [
        (
            'readme = "README.md"',
            'license = "Apache-2.0"',
            """
authors = [
    { name = "Alice", email = "alice@example.com" },
    { name = "Bob" },
    { email = "charlie@example.com" },
    {},
]
maintainers = [{ name = "Maintainer One" }]
""",
            (
                "Description-Content-Type: text/markdown",
                "License-Expression: Apache-2.0",
                "Author: Bob",
                "Author-email: Alice <alice@example.com>, charlie@example.com",
                "Maintainer: Maintainer One",
                "# Readme Body",
            ),
        ),
        (
            'readme = { file = "README.rst", content-type = "text/x-rst" }',
            'license = { text = "MIT\\nLicense Text" }',
            "",
            (
                "Description-Content-Type: text/x-rst",
                "License: MIT License Text",
            ),
        ),
        (
            'readme = { text = "Inline readme", content-type = "text/plain" }',
            'license = { file = "NOTICE" }\nlicense-files = ["LICENSE", "NOTICE"]',
            "",
            (
                "Description-Content-Type: text/plain",
                "License-File: LICENSE",
                "License-File: NOTICE",
                "Inline readme",
            ),
        ),
        (
            "",
            'license = { file = "EXTRA_LICENSE" }\nlicense-files = ["LICENSE"]',
            "",
            (
                "License-File: LICENSE",
                "License-File: EXTRA_LICENSE",
            ),
        ),
        (
            "",
            "license = {}",
            "",
            ("Metadata-Version: 2.4",),
        ),
    ],
)
def test_build_sdist_metadata_variations_and_walk_fallback(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    readme_toml: str,
    license_toml: str,
    people_toml: str,
    expected_metadata_substrings: Sequence[str],
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "README.md").write_text("# Readme Body\n", encoding="utf-8")
    (tmp_path / "README.rst").write_text("Readme RST\n", encoding="utf-8")
    (tmp_path / "LICENSE").write_text("Apache\n", encoding="utf-8")
    (tmp_path / "NOTICE").write_text("Notice\n", encoding="utf-8")
    (tmp_path / "EXTRA_LICENSE").write_text("Extra\n", encoding="utf-8")
    (tmp_path / "bytecode.pyc").write_bytes(b"\x00")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "bazel-bin").mkdir()
    (tmp_path / "bazel-bin" / "ignored.so").write_bytes(b"\x7fELF")

    (tmp_path / "pyproject.toml").write_text(
        f"""
[project]
name = "My-Pkg.Test"
version = "0.2.0-rc1"
description = "Multi\\nline summary"
requires-python = ">=3.13"
keywords = ["bazel", "pep517"]
classifiers = ["Programming Language :: Python :: 3"]
dependencies = ["torch>=2.11"]
{readme_toml}
{license_toml}
{people_toml}

[project.urls]
Homepage = "https://example.com"

[project.optional-dependencies]
dev = ["pytest>=8", "ruff>=0.9 ; python_version >= '3.13'"]

[tool.bazel517]
wheel-target = "//ci/wheel:my_pkg_wheel"
""",
        encoding="utf-8",
    )

    sdist_out = tmp_path / "sdist_out"
    sdist_name = bazel517.build_sdist(str(sdist_out))
    assert sdist_name == "my_pkg_test-0.2.0_rc1.tar.gz"

    with tarfile.open(sdist_out / sdist_name, "r:gz") as tf:
        names = set(tf.getnames())
        assert "my_pkg_test-0.2.0_rc1/pyproject.toml" in names
        assert "my_pkg_test-0.2.0_rc1/bytecode.pyc" not in names
        assert "my_pkg_test-0.2.0_rc1/.venv/ignored.py" not in names
        assert "my_pkg_test-0.2.0_rc1/bazel-bin/ignored.so" not in names

        pkg_info_Member = tf.extractfile("my_pkg_test-0.2.0_rc1/PKG-INFO")
        assert pkg_info_Member is not None
        pkg_info = pkg_info_Member.read().decode("utf-8")
        for expected in expected_metadata_substrings:
            assert expected in pkg_info
        assert "Summary: Multi line summary\n" in pkg_info
        assert "Keywords: bazel,pep517\n" in pkg_info
        assert "Project-URL: Homepage, https://example.com\n" in pkg_info
        assert "Classifier: Programming Language :: Python :: 3\n" in pkg_info
        assert "Requires-Python: >=3.13\n" in pkg_info
        assert "Requires-Dist: torch>=2.11\n" in pkg_info
        assert 'Requires-Dist: pytest>=8; extra == "dev"\n' in pkg_info
        expected_ruff_req = (
            "Requires-Dist: ruff>=0.9; "
            "(python_version >= '3.13') and extra == \"dev\"\n"
        )
        assert expected_ruff_req in pkg_info


def test_build_sdist_git_tracked_and_fallback_when_git_fails(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        """
[project]
name = "gitpkg"
version = "1.0.0"

[tool.bazel517]
target = "//:wheel"
""",
        encoding="utf-8",
    )
    (tmp_path / "tracked.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "untracked.py").write_text("Y = 2\n", encoding="utf-8")

    subprocess.run(["git", "init"], cwd=tmp_path, check=True, capture_output=True)
    subprocess.run(
        ["git", "config", "user.email", "test@example.com"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "config", "user.name", "Test User"],
        cwd=tmp_path,
        check=True,
    )
    subprocess.run(
        ["git", "add", "pyproject.toml", "tracked.py"],
        cwd=tmp_path,
        check=True,
    )
    # Delete tracked.py after staging to exercise the `if p.is_file():` branch
    (tmp_path / "deleted_staged.py").write_text("Z = 3\n", encoding="utf-8")
    subprocess.run(["git", "add", "deleted_staged.py"], cwd=tmp_path, check=True)
    (tmp_path / "deleted_staged.py").unlink()

    sdist_dir = tmp_path / "sdist1"
    sdist_name = bazel517.build_sdist(str(sdist_dir))
    with tarfile.open(sdist_dir / sdist_name, "r:gz") as tf:
        names = set(tf.getnames())
        assert "gitpkg-1.0.0/tracked.py" in names
        assert "gitpkg-1.0.0/untracked.py" not in names
        assert "gitpkg-1.0.0/deleted_staged.py" not in names

    # Corrupt .git into a plain file without a repo so `git ls-files` falls back
    subprocess.run(["rm", "-rf", str(tmp_path / ".git")], check=True)
    (tmp_path / ".git").write_text("not a valid gitdir pointer\n", encoding="utf-8")

    sdist_dir2 = tmp_path / "sdist2"
    sdist_name2 = bazel517.build_sdist(str(sdist_dir2))
    with tarfile.open(sdist_dir2 / sdist_name2, "r:gz") as tf:
        names2 = set(tf.getnames())
        assert "gitpkg-1.0.0/tracked.py" in names2
        assert "gitpkg-1.0.0/untracked.py" in names2


@hypothesis.settings(
    max_examples=25,
    suppress_health_check=[hypothesis.HealthCheck.function_scoped_fixture],
)
@hypothesis.given(
    raw_name=st.from_regex(r"[A-Za-z][A-Za-z0-9._-]{0,20}[A-Za-z0-9]", fullmatch=True),
    major=st.integers(min_value=0, max_value=99),
    minor=st.integers(min_value=0, max_value=99),
    patch=st.integers(min_value=0, max_value=99),
)
def test_sdist_filename_normalization_property(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    raw_name: str,
    major: int,
    minor: int,
    patch: int,
) -> None:
    case_dir = tmp_path / f"case_{major}_{minor}_{patch}"
    case_dir.mkdir(exist_ok=True)
    monkeypatch.chdir(case_dir)
    version = f"{major}.{minor}.{patch}-post1"
    (case_dir / "pyproject.toml").write_text(
        f"""
[project]
name = {raw_name!r}
version = {version!r}
""",
        encoding="utf-8",
    )
    out_dir = case_dir / "out"
    sdist_name = bazel517.build_sdist(
        str(out_dir),
        config_settings={"target": "//:whl"},
    )
    assert sdist_name.endswith(".tar.gz")
    stem = sdist_name.removesuffix(".tar.gz")
    name_part, ver_part = stem.split("-", maxsplit=1)
    assert "-" not in name_part
    assert "." not in name_part
    assert name_part == name_part.lower()
    assert ver_part == f"{major}.{minor}.{patch}_post1"
