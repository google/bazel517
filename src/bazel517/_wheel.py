"""Wheel and PEP 660 editable wheel builders."""

from __future__ import annotations

import pathlib
import shutil
import zipfile

from . import _bazel, _config, _metadata


def build_standard_wheel(
    cfg: _config.BuildConfig,
    wheel_dir: pathlib.Path,
) -> str:
    """Build the Bazel wheel target and copy the resulting `.whl` into `wheel_dir`."""
    built_whl = _bazel.build_bazel_wheel_target(
        cfg.project_root,
        cfg.bazel,
        editable=False,
    )
    wheel_dir.mkdir(parents=True, exist_ok=True)
    dest_path = wheel_dir / built_whl.name
    shutil.copy2(built_whl, dest_path)
    return dest_path.name


def build_editable_wheel(
    cfg: _config.BuildConfig,
    wheel_dir: pathlib.Path,
) -> str:
    """Build a PEP 660 editable wheel using source paths queried directly from Bazel."""
    built_whl = _bazel.build_bazel_wheel_target(
        cfg.project_root,
        cfg.bazel,
        editable=True,
    )
    layout = _bazel.query_wheel_source_layout(
        cfg.project_root,
        cfg.bazel,
        editable=True,
    )

    wheel_dir.mkdir(parents=True, exist_ok=True)
    dest_path = wheel_dir / built_whl.name

    norm_name = _config.distribution_filename_component(cfg.metadata.name)
    pth_arcname = f"_editable_{norm_name}.pth"
    pth_bytes = "".join(f"{p}\n" for p in layout.pth_dirs).encode()

    record_entries: list[tuple[str, str, int | str]] = []
    record_arcname: str | None = None

    with (
        zipfile.ZipFile(built_whl, mode="r") as src_zf,
        zipfile.ZipFile(
            dest_path, mode="w", compression=zipfile.ZIP_DEFLATED
        ) as dst_zf,
    ):
        for info in src_zf.infolist():
            arcname = info.filename
            if arcname.endswith("/"):
                continue
            if arcname.endswith(".dist-info/RECORD"):
                record_arcname = arcname
                continue
            if arcname in layout.source_arcnames and pathlib.PurePosixPath(
                arcname
            ).suffix in {".py", ".pyi"}:
                continue

            data = src_zf.read(arcname)
            dst_zf.writestr(info, data)
            digest, size = _metadata.record_hash(data)
            record_entries.append((arcname, digest, size))

        dst_zf.writestr(pth_arcname, pth_bytes)
        pth_digest, pth_size = _metadata.record_hash(pth_bytes)
        record_entries.append((pth_arcname, pth_digest, pth_size))

        if record_arcname is None:
            norm_ver = _config.version_filename_component(cfg.metadata.version)
            record_arcname = f"{norm_name}-{norm_ver}.dist-info/RECORD"
        record_bytes = _metadata.format_record(record_entries, record_arcname).encode()
        dst_zf.writestr(record_arcname, record_bytes)

    return dest_path.name
