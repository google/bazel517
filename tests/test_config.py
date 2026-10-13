from __future__ import annotations

from collections.abc import Callable
import pathlib

import pytest

import bazel517


@pytest.mark.parametrize(
    ("toml_content", "expected_exc", "expected_match"),
    [
        (None, FileNotFoundError, "pyproject.toml not found"),
        ("not_project = 1\n", ValueError, r"Missing \[project\] table"),
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
            '[project]\nname = "pkg"\nversion = "not-a-pep440-version"\n',
            ValueError,
            "Invalid version",
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
    ],
)
def test_invalid_sdist_pyproject_configs(
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
    ("toml_content", "expected_exc", "expected_match"),
    [
        (None, FileNotFoundError, "pyproject.toml not found"),
        (
            '[tool]\nbazel517 = "not-a-table"\n',
            ValueError,
            r"\[tool\.bazel517\] must be a TOML table",
        ),
        (
            "[tool.bazel517]\n",
            ValueError,
            "Missing required Bazel wheel target",
        ),
    ],
)
def test_invalid_wheel_pyproject_configs(
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
        bazel517.build_wheel(str(tmp_path / "wheels"))


def test_build_wheel_and_editable_without_static_project_metadata(
    tmp_path: pathlib.Path,
    monkeypatch: pytest.MonkeyPatch,
    fake_bazel_factory: Callable[[pathlib.Path, pathlib.Path], pathlib.Path],
) -> None:
    proj = tmp_path / "dynamic_proj"
    pkg = proj / "src" / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("X = 1\n", encoding="utf-8")
    fake_bazel = fake_bazel_factory(tmp_path, proj)

    (proj / "pyproject.toml").write_text(
        f"""
[build-system]
requires = ["bazel517"]
build-backend = "bazel517"

[project]
name = "mypkg"
dynamic = ["version"]

[tool.bazel517]
bazel-bin = {str(fake_bazel)!r}
wheel-target = "//ci/wheel:mypkg_wheel"
""",
        encoding="utf-8",
    )
    monkeypatch.chdir(proj)

    wheel_dir = tmp_path / "wheel_out"
    whl_name = bazel517.build_wheel(
        str(wheel_dir),
        config_settings={"target": "//ci/wheel:mypkg_wheel"},
    )
    assert (wheel_dir / whl_name).is_file()

    edit_dir = tmp_path / "edit_out"
    edit_name = bazel517.build_editable(str(edit_dir))
    assert (edit_dir / edit_name).is_file()
