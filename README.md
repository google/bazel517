# `bazel517`

A modern, `setuptools`-free Python build backend (PEP 517 / PEP 660) that wraps a Bazel wheel target (`py_wheel`).

`bazel517` enables standard `pip install .`, `pip install -e .`, `uv pip install .`, and `python -m build` workflows for projects whose wheels are built by Bazel (such as [`google-pytorch/torch_tpu`](https://github.com/google-pytorch/torch_tpu)) without relying on `setuptools` or `setup.py`.

## How It Works

1. **`build_wheel`**: Invokes `bazel build` with `--build_event_json_file` on your configured Bazel wheel target (e.g. `//ci/wheel:torch_tpu_wheel`), extracts the exact `.whl` output path from the Build Event Protocol stream in a single Bazel invocation, copies the built `.whl` into the PEP 517 `wheel_directory`, and returns its filename.
2. **`build_editable` (PEP 660)**: Builds the Bazel wheel target and queries the `PyWheel` action directly via `bazel aquery` (using the canonicalized build flags resolved by `bazel build`) to discover which wheel entries correspond to workspace source files and what their workspace root directories are. It strips those `.py`/`.pyi` workspace source files from the wheel while keeping all compiled artifacts (`.so`, generated files, `.dist-info`), writes a `.pth` file and `_editable_<pkg>.py` `MetaPathFinder` that merges workspace source directories with installed `site-packages` package directories on `__path__`, and updates `RECORD`.
3. **`build_sdist`**: Builds a PEP 625 source distribution tarball (`.tar.gz`) with `PKG-INFO` from your repository sources.

## Usage in `pyproject.toml`

```toml
[build-system]
requires = ["bazel517"]
build-backend = "bazel517"

[project]
name = "torch_tpu"
version = "0.2.1"
requires-python = ">=3.11"

[tool.bazel517]
target = "//ci/wheel:torch_tpu_wheel"
bazel-args = [
    "-c", "opt",
    "--config=wheel_common",
    "--repo_env=HERMETIC_PYTHON_VERSION={python_version}",
    "--define=PYTHON_VERSION={python_version_abi}",
    "--@rules_python//python/config_settings:py_freethreaded={freethreaded}",
]
```

### Template Placeholders

Arguments in `bazel-args`, `editable-bazel-args`, and `env` support automatic interpolation for the active Python environment:

- `{python_version}`: e.g. `"3.13"`
- `{python_version_nodot}`: e.g. `"313"`
- `{python_version_abi}`: e.g. `"3.13"` or `"3.14t"` (for free-threaded builds)
- `{python_abi_tag}`: e.g. `"cp313"` or `"cp314t"`
- `{freethreaded}`: `"yes"` or `"no"`
- `{python_executable}`: full path to `sys.executable`

### Passing Extra Flags via `config_settings` or Environment Variables

You can pass extra Bazel flags at build/install time via PEP 517 `--config-settings` (`-C`) or `BAZEL517_BAZEL_ARGS`:

```bash
pip install . -C bazel-args="--config=local_torch" -C bazel-args="--config=no_rbe"
uv pip install -e . -C bazel-args="--config=local_torch"
BAZEL517_BAZEL_ARGS="--config=no_rbe" pip install .
```
