from __future__ import annotations

import io
import json
import tempfile
import unittest
from pathlib import Path
from zipfile import ZipFile

from backend.tests.test_build import write_packages
from build import ARTIFACTS, PROFILES, BuildError
from release_assets import package_release, split_asset


class ReleaseAssetsTests(unittest.TestCase):
    def test_small_asset_stays_intact_and_large_assets_reassemble_exactly(self) -> None:
        for size in (15, 16, 17, 32, 33):
            with self.subTest(size=size), tempfile.TemporaryDirectory() as directory:
                archive = Path(directory) / "bundle.zip"
                original = bytes(range(size))
                archive.write_bytes(original)
                parts = split_asset(archive, max_bytes=16)
                self.assertEqual(b"".join(part.read_bytes() for part in parts), original)
                self.assertTrue(all(0 < part.stat().st_size <= 16 for part in parts))
                if size <= 16:
                    self.assertEqual(parts, [archive])
                else:
                    self.assertFalse(archive.exists())
                    self.assertEqual(
                        [part.name for part in parts],
                        [f"bundle.zip.{index:03d}" for index in range(1, len(parts) + 1)],
                    )

    def test_cpu_and_cuda_archives_have_the_requested_runtime_and_exclude_user_files(self) -> None:
        for device in ("cpu", "cuda"):
            with self.subTest(device=device), tempfile.TemporaryDirectory() as directory:
                repository = Path(directory)
                distribution = repository / "dist"
                write_packages(distribution, "new")
                for profile in PROFILES:
                    package = distribution / profile
                    (package / ".env").write_text("API_KEY=private", encoding="utf-8")
                    (package / "data").mkdir()
                    (package / "data" / "private.txt").write_text("user data", encoding="utf-8")
                    (package / "notes.txt").write_text("user notes", encoding="utf-8")
                    (package / "public" / ".hidden-asset").write_text("asset", encoding="utf-8")
                    (package / "python-kokoro" / "runtime.json").write_text(
                        json.dumps({"device": device}), encoding="utf-8",
                    )
                    if device == "cuda":
                        libraries = package / "python-kokoro" / "_internal" / "torch" / "lib"
                        (libraries / "torch_cuda.dll").write_bytes(b"CUDA runtime")
                        (libraries / "cudart64_12.dll").write_bytes(b"CUDA runtime")
                assets = package_release("2026.01.020304", device, repository=repository, max_asset_bytes=200)
                self.assertTrue(all(asset.stat().st_size <= 200 for asset in assets))
                for profile in PROFILES:
                    prefix = f"the-connector-2026.01.020304-windows-x64-{device}-{profile.lower()}.zip"
                    parts = sorted(asset for asset in assets if asset.name.startswith(prefix))
                    with ZipFile(io.BytesIO(b"".join(part.read_bytes() for part in parts))) as zipped:
                        self.assertIsNone(zipped.testzip())
                        self.assertEqual(
                            json.loads(zipped.read(f"{profile}/python-kokoro/runtime.json")),
                            {"device": device},
                        )
                        for artifact in ARTIFACTS:
                            self.assertTrue(any(name.startswith(f"{profile}/{artifact}") for name in zipped.namelist()))
                        self.assertIn(f"{profile}/config.json", zipped.namelist())
                        self.assertIn(f"{profile}/public/.hidden-asset", zipped.namelist())
                        self.assertNotIn(f"{profile}/.env", zipped.namelist())
                        self.assertNotIn(f"{profile}/data/private.txt", zipped.namelist())
                        self.assertNotIn(f"{profile}/notes.txt", zipped.namelist())
                        if device == "cuda":
                            self.assertIn(f"{profile}/python-kokoro/_internal/torch/lib/torch_cuda.dll", zipped.namelist())

    def test_mismatched_device_and_invalid_versions_cannot_be_released(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            write_packages(repository / "dist", "new")
            with self.assertRaisesRegex(BuildError, "requested cuda bundle"):
                package_release("2026.01.020304", "cuda", repository=repository)
            self.assertEqual(list((repository / "dist" / "release-assets").iterdir()), [])
            for version in ("v2026.01.020304", "2026.1.020304", "../../outside"):
                with self.subTest(version=version), self.assertRaisesRegex(BuildError, "Version"):
                    package_release(version, "cpu", repository=repository)


if __name__ == "__main__":
    unittest.main()
