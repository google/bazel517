"""Core Metadata (PEP 621 / PEP 639) and wheel RECORD formatting."""

from __future__ import annotations

import base64
from collections.abc import Mapping, Sequence
import csv
import hashlib
import io

from . import _config


def _format_people(
    entries: Sequence[Mapping[str, str]],
    name_header: str,
    email_header: str,
) -> tuple[str, ...]:
    names: list[str] = []
    emails: list[str] = []
    for person in entries:
        name = person.get("name")
        email = person.get("email")
        if name and email:
            emails.append(f"{name} <{email}>")
        elif email:
            emails.append(email)
        elif name:
            names.append(name)
    lines: list[str] = []
    if names:
        lines.append(f"{name_header}: {', '.join(names)}")
    if emails:
        lines.append(f"{email_header}: {', '.join(emails)}")
    return tuple(lines)


def format_core_metadata(metadata: _config.ProjectMetadata) -> str:
    """Format PEP 621 / PEP 639 Core Metadata 2.4 (`METADATA` / `PKG-INFO`)."""
    lines: list[str] = [
        "Metadata-Version: 2.4",
        f"Name: {metadata.name}",
        f"Version: {metadata.version}",
    ]
    if metadata.description:
        summary = " ".join(metadata.description.splitlines()).strip()
        lines.append(f"Summary: {summary}")

    lines.extend(_format_people(metadata.authors, "Author", "Author-email"))
    lines.extend(_format_people(metadata.maintainers, "Maintainer", "Maintainer-email"))

    if metadata.license_expression:
        lines.append(f"License-Expression: {metadata.license_expression}")
    elif metadata.license_text:
        single_line = " ".join(metadata.license_text.splitlines()).strip()
        lines.append(f"License: {single_line}")

    for lic_file in metadata.license_files:
        lines.append(f"License-File: {lic_file}")

    if metadata.keywords:
        lines.append(f"Keywords: {','.join(metadata.keywords)}")

    for label, url in metadata.urls.items():
        lines.append(f"Project-URL: {label}, {url}")

    for classifier in metadata.classifiers:
        lines.append(f"Classifier: {classifier}")

    if metadata.requires_python:
        lines.append(f"Requires-Python: {metadata.requires_python}")

    if metadata.readme_content_type:
        lines.append(f"Description-Content-Type: {metadata.readme_content_type}")

    for dep in metadata.dependencies:
        lines.append(f"Requires-Dist: {dep}")

    for extra, extra_deps in metadata.optional_dependencies.items():
        lines.append(f"Provides-Extra: {extra}")
        for dep in extra_deps:
            if ";" in dep:
                req_part, marker_part = dep.split(";", 1)
                req_clean = req_part.strip()
                marker_clean = marker_part.strip()
                combined_marker = f'({marker_clean}) and extra == "{extra}"'
                lines.append(f"Requires-Dist: {req_clean}; {combined_marker}")
            else:
                lines.append(f'Requires-Dist: {dep}; extra == "{extra}"')

    header_block = "\n".join(lines) + "\n"
    if metadata.readme_text is not None:
        return f"{header_block}\n{metadata.readme_text.rstrip()}\n"
    return header_block


def record_hash(data: bytes) -> tuple[str, int]:
    """Return `(sha256=<urlsafe-b64-nopad>, len(data))` for a RECORD entry."""
    digest = hashlib.sha256(data).digest()
    encoded = base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")
    return f"sha256={encoded}", len(data)


def format_record(
    entries: Sequence[tuple[str, str, int | str]],
    record_path: str,
) -> str:
    """Format a `.dist-info/RECORD` file according to PEP 376 / PEP 427."""
    buf = io.StringIO()
    writer = csv.writer(buf, delimiter=",", quotechar='"', lineterminator="\n")
    for path, digest, size in entries:
        writer.writerow([path, digest, str(size)])
    writer.writerow([record_path, "", ""])
    return buf.getvalue()
