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
import pathlib
import stat
import subprocess
import sys
from typing import Any, Protocol

import pytest


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
    ) -> pathlib.Path: ...


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
) -> pathlib.Path:
    """Create an executable script simulating `bazel build` (with BEP) and `aquery`."""
    fake_bazel = bin_dir / "fake_bazel"
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

    script = f"""#!{sys.executable}
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
custom_bep_lines = {list(bep_lines) if bep_lines is not None else None!r}

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

bep_path = None
for arg in args:
    if arg.startswith("--build_event_json_file="):
        bep_path = pathlib.Path(arg.split("=", 1)[1])

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
            zf.writestr("mypkg/_generated.py", "GENERATED = True\\n")
            zf.writestr("mypkg/data.json", "{{}}\\n")
            zf.writestr("mypkg/_ext.so", b"\\x7fELF-fake-shared-object")
            zf.writestr("mypkg/_ext.pyd", b"MZ-fake-windows-pyd")
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
    if bep_path is not None:
        if custom_bep_lines is not None:
            bep_path.write_text("\\n".join(custom_bep_lines) + "\\n", encoding="utf-8")
        else:
            default_event = {{
                "id": {{"namedSet": {{"id": "0"}}}},
                "namedSetOfFiles": {{
                    "files": [
                        {{
                            "name": out_whl.name,
                            "uri": out_whl.as_uri(),
                        }}
                    ]
                }},
            }}
            bep_path.write_text(json.dumps(default_event) + "\\n", encoding="utf-8")
elif cmd == "aquery":
    print(json.dumps({{"actions": {actions_list!r}}}))
else:
    sys.exit(2)
"""
    if sys.platform == "win32":
        script_py = bin_dir / "_fake_bazel_impl.py"
        script_py.write_text(script, encoding="utf-8")
        fake_bazel_exe = bin_dir / "fake_bazel.exe"
        if not fake_bazel_exe.is_file():
            cs_path = bin_dir / "_fake_bazel_wrapper.cs"
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
                [csc, "/nologo", f"/out:{fake_bazel_exe}", str(cs_path)],
                check=True,
                capture_output=True,
            )
        return fake_bazel_exe

    fake_bazel.write_text(script, encoding="utf-8")
    fake_bazel.chmod(fake_bazel.stat().st_mode | stat.S_IEXEC)
    return fake_bazel


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
requires-python = ">=3.11"

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
