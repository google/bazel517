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

from collections.abc import Mapping, Sequence
import json
import pathlib
import stat
import subprocess
import sys
import textwrap
from typing import Any, Final, Protocol

import pytest

_DATA_DIR: Final[pathlib.Path] = pathlib.Path(__file__).resolve().parent / "data"
_FAKE_BAZEL_SCRIPT: Final[pathlib.Path] = _DATA_DIR / "fake_bazel.py"


class FakeBazelFactory(Protocol):
    def __call__(
        self,
        bin_dir: pathlib.Path,
        workspace_dir: pathlib.Path,
        *,
        bep_lines: Sequence[str] | None = None,
        create_wheel_file: bool = True,
        include_dir_entry: bool = False,
        include_record: bool = True,
        aquery_actions: Sequence[Mapping[str, Any]] | None = None,
        write_inputs_txt: bool = True,
        extra_input_lines: Sequence[str] = (),
        bazel_version: str = "8.6.0",
        installed_versions: Sequence[str] | None = None,
        log_dir: pathlib.Path | None = None,
    ) -> pathlib.Path: ...


def build_python_shim(
    bin_dir: pathlib.Path,
    name: str,
    script_path: pathlib.Path,
    config_path: pathlib.Path,
) -> pathlib.Path:
    """Create a standalone executable shim in `bin_dir` invoking `script_path`.

    Passes `config_path` as `sys.argv[1]` followed by any CLI arguments passed
    to the shim. Compiles a native `.exe` via `csc.exe` on Windows (where
    `shutil.which` and `CreateProcess` require an executable extension) and
    writes a `#!` script on POSIX.
    """
    bin_dir.mkdir(parents=True, exist_ok=True)
    if sys.platform == "win32":
        exe_path = bin_dir / f"{name}.exe"
        if not exe_path.is_file():
            cs_path = bin_dir / f"_{name}_shim.cs"
            py_escaped = sys.executable.replace("\\", "\\\\")
            script_escaped = str(script_path).replace("\\", "\\\\")
            config_escaped = str(config_path).replace("\\", "\\\\")
            cs_source = textwrap.dedent(
                f"""\
                using System;
                using System.Diagnostics;
                class P {{
                    static int Main() {{
                        string cmd = Environment.CommandLine;
                        int i = 0;
                        if (cmd.StartsWith("\\"")) {{
                            i = cmd.IndexOf('"', 1) + 1;
                        }} else {{
                            while (i < cmd.Length && !char.IsWhiteSpace(cmd[i]))
                                i++;
                        }}
                        string rest = cmd.Substring(i);
                        string py = "{py_escaped}";
                        string prefix = "\\"{script_escaped}\\" \\"{config_escaped}\\"";
                        string args = prefix + rest;
                        var psi = new ProcessStartInfo(py, args) {{
                            UseShellExecute = false
                        }};
                        var p = Process.Start(psi);
                        p.WaitForExit();
                        return p.ExitCode;
                    }}
                }}
                """
            )
            cs_path.write_text(cs_source, encoding="utf-8")
            csc = r"C:\Windows\Microsoft.NET\Framework64\v4.0.30319\csc.exe"
            subprocess.run(
                [csc, "/nologo", f"/out:{exe_path}", str(cs_path)],
                check=True,
                capture_output=True,
            )
        return exe_path

    shim_path = bin_dir / name
    shim_source = textwrap.dedent(
        f"""\
        #!{sys.executable}
        import runpy
        import sys

        sys.argv = [{str(script_path)!r}, {str(config_path)!r}, *sys.argv[1:]]
        runpy.run_path({str(script_path)!r}, run_name="__main__")
        """
    )
    shim_path.write_text(shim_source, encoding="utf-8")
    shim_path.chmod(shim_path.stat().st_mode | stat.S_IEXEC)
    return shim_path


def _write_fake_bazel(
    bin_dir: pathlib.Path,
    workspace_dir: pathlib.Path,
    *,
    bep_lines: Sequence[str] | None = None,
    create_wheel_file: bool = True,
    include_dir_entry: bool = False,
    include_record: bool = True,
    aquery_actions: Sequence[Mapping[str, Any]] | None = None,
    write_inputs_txt: bool = True,
    extra_input_lines: Sequence[str] = (),
    bazel_version: str = "8.6.0",
    installed_versions: Sequence[str] | None = None,
    log_dir: pathlib.Path | None = None,
) -> pathlib.Path:
    """Create an executable shim simulating `bazel build` (with BEP) and `aquery`."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    effective_log_dir = log_dir if log_dir is not None else bin_dir
    wheel_bin_dir = workspace_dir / "bazel-out" / "k8-opt" / "bin" / "ci" / "wheel"
    out_whl = wheel_bin_dir / "mypkg-0.1.0-cp313-cp313-linux_x86_64.whl"
    inputs_txt = wheel_bin_dir / "mypkg_wheel_target_wrapped_inputs.txt"

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
        "ci/wheel/pkg_stage/mypkg/_generated.py;"
        "bazel-out/k8-opt/bin/ci/wheel/pkg_stage/mypkg/_generated.py",
        "ci/wheel/pkg_stage/mypkg/_ext.so;"
        "bazel-out/k8-opt/bin/ci/wheel/pkg_stage/mypkg/_ext.so",
        "ci/wheel/pkg_stage/mypkg/_ext.pyd;"
        "bazel-out/k8-opt/bin/ci/wheel/pkg_stage/mypkg/_ext.pyd",
        *extra_input_lines,
    ]

    config_path = bin_dir / "_fake_bazel_config.json"
    config_payload = {
        "out_whl": str(out_whl),
        "inputs_txt": str(inputs_txt),
        "log_file": str(effective_log_dir / "bazel_invocations.jsonl"),
        "bep_lines": list(bep_lines) if bep_lines is not None else None,
        "default_version": bazel_version,
        "installed_versions": (
            list(installed_versions) if installed_versions is not None else None
        ),
        "write_inputs_txt": write_inputs_txt,
        "input_lines": input_lines,
        "create_wheel_file": create_wheel_file,
        "include_dir_entry": include_dir_entry,
        "include_record": include_record,
        "actions_list": actions_list,
    }
    config_path.write_text(json.dumps(config_payload), encoding="utf-8")

    return build_python_shim(
        bin_dir=bin_dir,
        name="fake_bazel",
        script_path=_FAKE_BAZEL_SCRIPT,
        config_path=config_path,
    )


@pytest.fixture
def fake_bazel_factory() -> FakeBazelFactory:
    return _write_fake_bazel


@pytest.fixture
def project_dir(
    tmp_path: pathlib.Path,
    fake_bazel_factory: FakeBazelFactory,
) -> pathlib.Path:
    proj = tmp_path / "proj"
    pkg = proj / "src" / "mypkg"
    pkg.mkdir(parents=True)
    (pkg / "__init__.py").write_text("X = 42\n", encoding="utf-8")
    (pkg / "data.json").write_text("{}\n", encoding="utf-8")
    (proj / "root_mod.py").write_text("ROOT = True\n", encoding="utf-8")
    (proj / "LICENSE").write_text("Apache-2.0\n", encoding="utf-8")
    fake_bazel = fake_bazel_factory(tmp_path, proj)

    (proj / "pyproject.toml").write_text(
        textwrap.dedent(
            f"""\
            [build-system]
            requires = ["bazel517"]
            build-backend = "bazel517"

            [project]
            name = "mypkg"
            version = "0.1.0"
            description = "Test package"
            license = "Apache-2.0"
            license-files = ["LICENSE"]
            requires-python = ">=3.11"

            [tool.bazel517]
            bazel-bin = {str(fake_bazel)!r}
            target = "//ci/wheel:mypkg_wheel"
            bazel-args = ["-c", "opt", "--define=py={{python_version}}"]
            editable-bazel-args = [
                "--config=editable",
                "--define=abi={{python_abi_tag}}",
            ]

            [tool.bazel517.env]
            CUSTOM_BAZEL_VAR = "py-{{python_version_nodot}}-{{freethreaded}}"
            """
        ),
        encoding="utf-8",
    )
    return proj
