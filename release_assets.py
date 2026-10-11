#!/usr/bin/env python3
"""Archive portable builds, splitting large ZIPs for GitHub Releases."""

from __future__ import annotations

import argparse
import re
import sys
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

from build import (
    ARTIFACTS, INITIAL_CONFIGURATION, PROFILES, ROOT, BuildError,
    checked_path, kokoro_bundle_device, validate_artifacts,
)

# GitHub requires each release asset to be smaller than 2 GiB.
ASSET_BYTES = 1900 * 1024 * 1024
COPY_BYTES = 1024 * 1024


def split_asset(archive: Path, *, max_bytes: int = ASSET_BYTES) -> list[Path]:
    """Keep small ZIPs intact; emit numbered parts that 7-Zip can open."""
    if max_bytes <= 0:
        raise ValueError("Asset size must be positive")
    if archive.stat().st_size <= max_bytes:
        return [archive]
    parts = []
    with archive.open("rb") as source:
        while chunk := source.read(min(COPY_BYTES, max_bytes)):
            part = archive.with_name(f"{archive.name}.{len(parts) + 1:03d}")
            with part.open("xb") as output:
                written = 0
                while chunk:
                    output.write(chunk)
                    written += len(chunk)
                    if written == max_bytes:
                        break
                    chunk = source.read(min(COPY_BYTES, max_bytes - written))
            parts.append(part)
    archive.unlink()
    return parts


def package_release(
    version: str, device: str, *, repository: Path = ROOT,
    max_asset_bytes: int = ASSET_BYTES,
) -> list[Path]:
    if not re.fullmatch(r"\d{4}\.\d{2}\.\d{6}", version):
        raise BuildError("Version must use YYYY.mm.ddHHmm format")
    if device not in ("cpu", "cuda"):
        raise BuildError("Speech device must be cpu or cuda")
    distribution = checked_path(repository / "dist", repository)
    output = checked_path(distribution / "release-assets", distribution)
    output.mkdir(parents=True, exist_ok=True)
    assets = []
    for profile, host in PROFILES.items():
        package = checked_path(distribution / profile, distribution)
        validate_artifacts(package, host=host)
        if kokoro_bundle_device(package / "python-kokoro") != device:
            raise BuildError(f"{profile} does not contain the requested {device} bundle")
        archive = output / f"the-connector-{version}-windows-x64-{device}-{profile.lower()}.zip"
        with ZipFile(archive, "x", compression=ZIP_DEFLATED, compresslevel=6) as zipped:
            # Package application artifacts, leaving out .env, data, and notes.
            for name in (*ARTIFACTS, INITIAL_CONFIGURATION):
                member = checked_path(package / name, package)
                files = sorted(member.rglob("*")) if member.is_dir() else [member]
                for file in files:
                    if file.is_file():
                        checked_path(file, package)
                        zipped.write(file, file.relative_to(distribution).as_posix())
        assets.extend(split_asset(archive, max_bytes=max_asset_bytes))
    return assets


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", required=True)
    parser.add_argument("--device", required=True, choices=("cpu", "cuda"))
    options = parser.parse_args(argv)
    for asset in package_release(options.version, options.device):
        print(f"[release] {asset.name} ({asset.stat().st_size:,} bytes)", flush=True)
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except (BuildError, OSError) as error:
        print(f"[release] ERROR: {error}", file=sys.stderr)
        raise SystemExit(1)
