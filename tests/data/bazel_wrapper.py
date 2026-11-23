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

"""Standalone multi-version `bazel` wrapper script used by integration tests."""

from __future__ import annotations

import json
import os
import pathlib
import subprocess
import sys
from typing import Final

_WORKSPACE_MARKERS: Final[tuple[str, ...]] = (
    "MODULE.bazel",
    "WORKSPACE",
    "WORKSPACE.bazel",
)


def _find_workspace_root(start: pathlib.Path) -> pathlib.Path | None:
    for directory in (start, *start.parents):
        if any((directory / marker).exists() for marker in _WORKSPACE_MARKERS):
            return directory
    return None


def _read_bazelversion(workspace_root: pathlib.Path) -> str | None:
    bazelversion = workspace_root / ".bazelversion"
    if not bazelversion.is_file():
        return None
    for raw_line in bazelversion.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if line and not line.startswith("#"):
            return line
    return None


def _resolve_requested_version(default_version: str) -> str:
    if env_req := os.environ.get("USE_BAZEL_VERSION", "").strip():
        return default_version if env_req == "latest" else env_req

    workspace_root = _find_workspace_root(pathlib.Path.cwd())
    if workspace_root is None:
        return default_version

    pinned = _read_bazelversion(workspace_root)
    if not pinned or pinned == "latest":
        return default_version
    return pinned


def main(argv: list[str]) -> int:
    config_path = pathlib.Path(argv[1])
    forwarded_args = argv[2:]
    config = json.loads(config_path.read_text(encoding="utf-8"))
    installed: dict[str, str] = config["installed"]
    default_version: str = config["default_version"]

    req = _resolve_requested_version(default_version)
    target = installed.get(req)
    if target is None:
        sys.stderr.write(
            f"ERROR: The project you're trying to build requires Bazel {req}, "
            f"but only {sorted(installed)} are installed.\n"
        )
        return 1

    return subprocess.call([target, *forwarded_args])


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
