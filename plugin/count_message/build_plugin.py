"""Build plugin.zip with source packages, instructions, and launchers only."""
from pathlib import Path
import argparse
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parent
EXCLUDED_DIRS = {"__pycache__", "node_modules", ".venv", "build", "dist", ".pytest_cache"}


def build(output=None):
    target = Path(output) if output else ROOT / "plugin.zip"
    files = [ROOT / name for name in (
        "instruction.md", "plugin.json", "run.py", "run.bat", "run.sh", "build_plugin.py", "LICENSE",
    )]
    for folder in ("ui", "backend"):
        files.extend(path for path in (ROOT / folder).rglob("*") if path.is_file()
                     and not any(part in EXCLUDED_DIRS or part.endswith(".egg-info") for part in path.relative_to(ROOT).parts)
                     and (path.name == "LICENSE" or path.suffix in {".py", ".toml", ".js", ".json", ".html", ".md"}))
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for path in sorted(files):
            info = ZipInfo(path.relative_to(ROOT).as_posix(), date_time=(2026, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.create_system = 3
            info.external_attr = (0o100755 if path.name == "run.sh" else 0o100644) << 16
            archive.writestr(info, path.read_bytes())
    return target


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path)
    print(build(parser.parse_args().output))
