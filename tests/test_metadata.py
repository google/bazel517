from __future__ import annotations

from collections.abc import Sequence
import pathlib
import tarfile

import pytest

import bazel517


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
            'readme = { file = "README.md" }',
            'license = { text = "MIT\\nLicense Text" }',
            "",
            (
                "Description-Content-Type: text/markdown",
                "License: MIT\n        License Text",
                "# Readme Body",
            ),
        ),
        (
            'readme = { file = "README.rst", content-type = "text/x-rst" }',
            'license = { text = "BSD-3-Clause" }',
            "",
            (
                "Description-Content-Type: text/x-rst",
                "License: BSD-3-Clause",
                "Readme RST",
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
            'readme = { text = "Inline default ct" }',
            'license = { file = "EXTRA_LICENSE" }\nlicense-files = ["LICENSE"]',
            "",
            (
                "Description-Content-Type: text/plain",
                "License-File: LICENSE",
                "License-File: EXTRA_LICENSE",
                "Inline default ct",
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
def test_sdist_core_metadata_variations(
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

    (tmp_path / "pyproject.toml").write_text(
        f"""
[project]
name = "My-Pkg.Test"
version = "0.2.0-rc1"
description = "Multi\\nline summary"
requires-python = ">=3.11"
keywords = ["bazel", "pep517"]
classifiers = ["Programming Language :: Python :: 3"]
dependencies = ["torch>=2.11"]
{readme_toml}
{license_toml}
{people_toml}

[project.urls]
Homepage = "https://example.com"

[project.optional-dependencies]
dev = ["pytest>=8", "ruff>=0.9 ; python_version >= '3.11'"]
""",
        encoding="utf-8",
    )

    sdist_out = tmp_path / "sdist_out"
    sdist_name = bazel517.build_sdist(str(sdist_out))
    assert sdist_name == "my_pkg_test-0.2.0rc1.tar.gz"

    with tarfile.open(sdist_out / sdist_name, "r:gz") as tf:
        pkg_info_member = tf.extractfile("my_pkg_test-0.2.0rc1/PKG-INFO")
        assert pkg_info_member is not None
        pkg_info = pkg_info_member.read().decode("utf-8")
        for expected in expected_metadata_substrings:
            assert expected in pkg_info
        assert "Version: 0.2.0rc1\n" in pkg_info
        assert "Summary: Multi line summary\n" in pkg_info
        assert "Keywords: bazel,pep517\n" in pkg_info
        assert "Project-URL: Homepage, https://example.com\n" in pkg_info
        assert "Classifier: Programming Language :: Python :: 3\n" in pkg_info
        assert "Requires-Python: >=3.11\n" in pkg_info
        assert "Requires-Dist: torch>=2.11\n" in pkg_info
        assert 'Requires-Dist: pytest>=8; extra == "dev"\n' in pkg_info
        expected_ruff_req = (
            "Requires-Dist: ruff>=0.9; "
            "(python_version >= '3.11') and extra == \"dev\"\n"
        )
        assert expected_ruff_req in pkg_info
