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

import pathlib
import shutil
import subprocess
import tarfile

import hypothesis
import hypothesis.strategies as st
import pytest

import bazel517


def test_build_sdist_walk_fallback_excludes_ignored_dirs_and_bytecode(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "walkpkg"\nversion = "1.0.0"\n',
        encoding="utf-8",
    )
    (tmp_path / "included.py").write_text("X = 1\n", encoding="utf-8")
    (tmp_path / "bytecode.pyc").write_bytes(b"\x00")
    (tmp_path / ".venv").mkdir()
    (tmp_path / ".venv" / "ignored.py").write_text("x = 1\n", encoding="utf-8")
    (tmp_path / "bazel-bin").mkdir()
    (tmp_path / "bazel-bin" / "ignored.so").write_bytes(b"\x7fELF")

    sdist_out = tmp_path / "sdist_out"
    sdist_name = bazel517.build_sdist(str(sdist_out))
    assert sdist_name == "walkpkg-1.0.0.tar.gz"

    with tarfile.open(sdist_out / sdist_name, "r:gz") as tf:
        names = set(tf.getnames())
        assert "walkpkg-1.0.0/pyproject.toml" in names
        assert "walkpkg-1.0.0/included.py" in names
        assert "walkpkg-1.0.0/bytecode.pyc" not in names
        assert "walkpkg-1.0.0/.venv/ignored.py" not in names
        assert "walkpkg-1.0.0/bazel-bin/ignored.so" not in names


def test_build_sdist_git_tracked_missing_git_binary_and_corrupt_git_repo(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    (tmp_path / "pyproject.toml").write_text(
        '[project]\nname = "gitpkg"\nversion = "1.0.0"\n',
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
    # Stage and unlink a file to exercise the `if p.is_file():` check
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

    # If the `git` binary is not installed (`FileNotFoundError`), fall back to walk
    with monkeypatch.context() as mp:
        mp.setenv("PATH", "")
        sdist_no_git = tmp_path / "sdist_no_git"
        sdist_name_no_git = bazel517.build_sdist(str(sdist_no_git))
        with tarfile.open(sdist_no_git / sdist_name_no_git, "r:gz") as tf:
            names_no_git = set(tf.getnames())
            assert "gitpkg-1.0.0/tracked.py" in names_no_git
            assert "gitpkg-1.0.0/untracked.py" in names_no_git

    # If `.git` exists but `git ls-files` fails, CalledProcessError must propagate
    for p in (tmp_path / ".git").rglob("*"):
        p.chmod(0o700)
    shutil.rmtree(tmp_path / ".git")
    (tmp_path / ".git").write_text("not a valid gitdir pointer\n", encoding="utf-8")

    with pytest.raises(subprocess.CalledProcessError):
        bazel517.build_sdist(str(tmp_path / "sdist_corrupt"))


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
    sdist_name = bazel517.build_sdist(str(out_dir))
    assert sdist_name.endswith(".tar.gz")
    stem = sdist_name.removesuffix(".tar.gz")
    name_part, ver_part = stem.split("-", maxsplit=1)
    assert "-" not in name_part
    assert "." not in name_part
    assert name_part == name_part.lower()
    assert ver_part == f"{major}.{minor}.{patch}.post1"
