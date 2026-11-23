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

"""Standalone fake `bazel` / `bazelisk` executable used by unit tests."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
import json
import os
import pathlib
import re
import sys
from typing import Any, Final
import zipfile

_PRERELEASE_SUFFIX_RE: Final[re.Pattern[str]] = re.compile(
    r"^(?:rc|pre|alpha|beta|dev|canary|snapshot)(?:[0-9._-]|$)",
    re.IGNORECASE,
)


def _read_requested_version(cwd: pathlib.Path) -> str | None:
    if env_req := os.environ.get("USE_BAZEL_VERSION", "").strip():
        return env_req

    bazeliskrc = cwd / ".bazeliskrc"
    if bazeliskrc.is_file():
        for raw_line in bazeliskrc.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line.startswith("USE_BAZEL_VERSION="):
                val = line.partition("=")[2].strip()
                if val:
                    return val
                break

    bazelversion = cwd / ".bazelversion"
    if bazelversion.is_file():
        for raw_line in bazelversion.read_text(encoding="utf-8").splitlines():
            line = raw_line.strip()
            if line and not line.startswith("#"):
                return line
    return None


def _candidate_matches(candidate: str, required: str) -> bool:
    if candidate == required:
        return True
    base, sep, suffix = candidate.partition("-")
    if not all(part.isdigit() for part in base.split(".")):
        return False
    if sep and _PRERELEASE_SUFFIX_RE.match(suffix):
        return False
    if base == required:
        return True
    if required.endswith((".x", ".*")):
        return base.startswith(required[:-1])
    return False


def _resolve_active_version(
    default_version: str,
    installed_versions: Sequence[str] | None,
) -> str:
    if installed_versions is None:
        return default_version

    req = _read_requested_version(pathlib.Path.cwd())
    if not req or req == "latest":
        return default_version

    if "*" in installed_versions:
        if req.endswith((".x", ".*")):
            return f"{req[:-2]}.0"
        return req

    for candidate in installed_versions:
        if _candidate_matches(candidate, req):
            return candidate

    sys.stderr.write(f"ERROR: Bazel {req} not found in {list(installed_versions)}\n")
    raise SystemExit(1)


def _read_and_check_bazelrc(cwd: pathlib.Path, active_version: str) -> list[str]:
    bazelrc_path = cwd / ".bazelrc"
    if not bazelrc_path.is_file():
        return []

    flags: list[str] = []
    for raw_line in bazelrc_path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#"):
            continue
        flags.append(line)
        for token in line.split():
            if not token.startswith("--require_bazel_major="):
                continue
            req_major = token.partition("=")[2]
            if not active_version.startswith(f"{req_major}."):
                sys.stderr.write(
                    f"ERROR: .bazelrc requires Bazel {req_major}.x, "
                    f"got {active_version}\n"
                )
                raise SystemExit(2)
    return flags


def _extract_bep_path(args: Sequence[str]) -> pathlib.Path | None:
    for arg in args:
        if arg.startswith("--build_event_json_file="):
            return pathlib.Path(arg.partition("=")[2])
    return None


def _handle_build(
    config: Mapping[str, Any],
    bep_path: pathlib.Path | None,
) -> int:
    out_whl = pathlib.Path(config["out_whl"])
    inputs_txt = pathlib.Path(config["inputs_txt"])
    out_whl.parent.mkdir(parents=True, exist_ok=True)

    if config["write_inputs_txt"]:
        input_lines: Sequence[str] = config["input_lines"]
        inputs_txt.write_text("\n".join(input_lines) + "\n", encoding="utf-8")

    if config["create_wheel_file"]:
        with zipfile.ZipFile(out_whl, "w") as zf:
            if config["include_dir_entry"]:
                zf.writestr("mypkg/", "")
            zf.writestr("mypkg/__init__.py", "X = 42\n")
            zf.writestr("mypkg/_generated.py", "GENERATED = True\n")
            zf.writestr("mypkg/data.json", "{}\n")
            zf.writestr("mypkg/_ext.so", b"\x7fELF-fake-shared-object")
            zf.writestr("mypkg/_ext.pyd", b"MZ-fake-windows-pyd")
            zf.writestr(
                "mypkg-0.1.0.dist-info/METADATA",
                "Metadata-Version: 2.4\nName: mypkg\nVersion: 0.1.0\n",
            )
            zf.writestr(
                "mypkg-0.1.0.dist-info/WHEEL",
                "Wheel-Version: 1.0\nTag: cp313-cp313-linux_x86_64\n",
            )
            if config["include_record"]:
                zf.writestr("mypkg-0.1.0.dist-info/RECORD", "")

    if bep_path is not None:
        custom_bep_lines: Sequence[str] | None = config["bep_lines"]
        if custom_bep_lines is not None:
            bep_path.write_text("\n".join(custom_bep_lines) + "\n", encoding="utf-8")
        else:
            default_event = {
                "id": {"namedSet": {"id": "0"}},
                "namedSetOfFiles": {
                    "files": [
                        {
                            "name": out_whl.name,
                            "uri": out_whl.as_uri(),
                        }
                    ]
                },
            }
            bep_path.write_text(json.dumps(default_event) + "\n", encoding="utf-8")
    return 0


def main(argv: list[str]) -> int:
    config = json.loads(pathlib.Path(argv[1]).read_text(encoding="utf-8"))
    args = argv[2:]
    cmd = args[0]

    active_version = _resolve_active_version(
        default_version=config["default_version"],
        installed_versions=config["installed_versions"],
    )

    if cmd == "--version":
        if active_version == "malformed":
            print("unrecognized version output")
            return 0
        print(f"bazel {active_version}")
        return 0

    bazelrc_flags = _read_and_check_bazelrc(pathlib.Path.cwd(), active_version)

    log_file = pathlib.Path(config["log_file"])
    with log_file.open("a", encoding="utf-8") as lf:
        lf.write(
            json.dumps(
                {
                    "cmd": cmd,
                    "args": args,
                    "custom_env": os.environ.get("CUSTOM_BAZEL_VAR"),
                    "bazel_version": active_version,
                    "bazelrc_flags": bazelrc_flags,
                }
            )
            + "\n"
        )

    if cmd == "build":
        return _handle_build(config, _extract_bep_path(args))
    if cmd == "aquery":
        print(json.dumps({"actions": config["actions_list"]}))
        return 0
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
