"""Stage the official model and voices for a self-contained Kokoro build.

Run with the isolated Python 3.12 build interpreter, passing the destination
directory. Cached Hugging Face files are reused before missing files download.
The resulting directory is bundled as ``kokoro-model`` by PyInstaller.
"""

from __future__ import annotations

import argparse
import shutil
from pathlib import Path

from huggingface_hub import hf_hub_download
from huggingface_hub.errors import LocalEntryNotFoundError

from model_assets import MODEL_REPO, VOICES


def prepare_assets(destination: Path) -> None:
    files = ("config.json", "kokoro-v1_0.pth", *(f"voices/{voice}.pt" for voice in VOICES))
    for filename in files:
        target = destination / filename
        if target.is_file() and target.stat().st_size > 0:
            continue
        try:
            source = hf_hub_download(MODEL_REPO, filename, local_files_only=True)
        except LocalEntryNotFoundError:
            source = hf_hub_download(MODEL_REPO, filename)
        target.parent.mkdir(parents=True, exist_ok=True)
        staged = target.with_name(target.name + ".tmp")
        try:
            shutil.copyfile(source, staged)
            staged.replace(target)
        finally:
            staged.unlink(missing_ok=True)
        print(f"[kokoro-build] Staged {filename}", flush=True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Stage Kokoro model and voice assets")
    parser.add_argument("destination", type=Path)
    args = parser.parse_args(argv)
    prepare_assets(args.destination.resolve())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
