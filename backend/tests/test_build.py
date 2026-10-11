from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock
from urllib.error import HTTPError

from build import (
    ARTIFACTS,
    INITIAL_CONFIGURATION,
    LAUNCHER,
    PROFILES,
    BuildError,
    checked_path,
    prepare_artifacts,
    publish_artifacts,
    pyinstaller_command,
    smoke_test,
    validate_artifacts,
    validate_port_defaults,
)


def write_packages(directory: Path, version: str) -> None:
    for profile, host in PROFILES.items():
        package = directory / profile
        package.mkdir(parents=True, exist_ok=True)
        (package / "server.exe").write_bytes(f"server-{profile}-{version}".encode("ascii"))
        (package / "public" / "assets").mkdir(parents=True)
        (package / "public" / "index.html").write_text(version, encoding="utf-8")
        (package / "public" / "assets" / "app.js").write_text(
            f"script-{profile}-{version}", encoding="utf-8",
        )
        (package / "run.bat").write_text(f"run-{profile}-{version}", encoding="ascii")
        # Different old defaults make runtime.json replacement and rollback observable.
        runtime_host = host if version == "new" else f"old-{host}"
        (package / "runtime.json").write_text(
            json.dumps({"host": runtime_host}), encoding="ascii",
        )
        ports = {"react_port": 5300, "connector_port": 8300, "kokoro_port": 8301}
        if version != "new":
            ports = {name: value + 100 for name, value in ports.items()}
        (package / "port_mapping.json").write_text(json.dumps(ports), encoding="utf-8")
        (package / INITIAL_CONFIGURATION).write_text(
            json.dumps({"version": version}), encoding="utf-8",
        )


def package_contents(directory: Path) -> dict[Path, bytes]:
    return {
        path.relative_to(directory): path.read_bytes()
        for path in directory.rglob("*") if path.is_file()
    }


def managed_contents(directory: Path) -> dict[str, dict[Path, bytes]]:
    return {profile: package_contents(directory / profile) for profile in PROFILES}


class ArtifactPreparationTests(unittest.TestCase):
    def test_portable_launcher_initializes_settings_before_starting_services(self) -> None:
        self.assertIn('"%~dp0server.exe" --initialize-settings', LAUNCHER)
        self.assertLess(LAUNCHER.index("--initialize-settings"), LAUNCHER.index('"%~dp0server.exe" %*'))
        self.assertIn("if errorlevel 1 exit /b %ERRORLEVEL%", LAUNCHER)

    def test_build_creates_two_self_contained_packages_with_mode_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            (stage / "server.exe").write_bytes(b"shared backend")
            (stage / "public" / "assets").mkdir(parents=True)
            (stage / "public" / "index.html").write_text("shared frontend", encoding="utf-8")
            (stage / "public" / "assets" / "app.js").write_bytes(b"shared script")
            (stage / INITIAL_CONFIGURATION).write_text('{"sequences": []}', encoding="utf-8")
            mapping = stage / "custom_ports.json"
            mapping.write_text('{"react_port": 5300, "connector_port": 8300, "kokoro_port": 8301}', encoding="utf-8")

            packages = prepare_artifacts(stage, defaults_path=mapping)

            self.assertEqual(packages, stage / "packages")
            self.assertEqual({path.name for path in packages.iterdir()}, {"Debug", "Release"})
            for profile, expected_host in {"Debug": "127.0.0.1", "Release": "0.0.0.0"}.items():
                with self.subTest(profile=profile):
                    package = packages / profile
                    validate_artifacts(package, host=expected_host)
                    self.assertEqual({path.name for path in package.iterdir()}, set(ARTIFACTS) | {INITIAL_CONFIGURATION})
                    self.assertEqual((package / "server.exe").read_bytes(), b"shared backend")
                    self.assertEqual(package_contents(package / "public"), package_contents(stage / "public"))
                    self.assertEqual((package / "run.bat").read_text(encoding="ascii"), LAUNCHER)
                    self.assertEqual(
                        json.loads((package / "runtime.json").read_text(encoding="ascii")),
                        {"host": expected_host},
                    )
                    self.assertFalse(any(path.is_symlink() for path in package.rglob("*")))
                    self.assertEqual((package / INITIAL_CONFIGURATION).read_bytes(), (stage / INITIAL_CONFIGURATION).read_bytes())
                    self.assertEqual((package / "port_mapping.json").read_bytes(), mapping.read_bytes())

            # Each mode remains usable after the shared staging files or the other mode change.
            (stage / "server.exe").unlink()
            (packages / "Debug" / "public" / "index.html").write_text("changed", encoding="utf-8")
            self.assertEqual((packages / "Release" / "server.exe").read_bytes(), b"shared backend")
            self.assertEqual(
                (packages / "Release" / "public" / "index.html").read_text(encoding="utf-8"),
                "shared frontend",
            )

    def test_validation_rejects_invalid_or_missing_port_mapping(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packages = Path(directory)
            write_packages(packages, "new")
            mapping = packages / "Debug" / "port_mapping.json"
            for content in ("{", "[]", "{}", '{"react_port": true, "connector_port": 8300, "kokoro_port": 8301}'):
                with self.subTest(content=content):
                    mapping.write_text(content, encoding="utf-8")
                    with self.assertRaisesRegex(BuildError, "port mapping"):
                        validate_artifacts(packages / "Debug", host=PROFILES["Debug"])
            mapping.unlink()
            with self.assertRaisesRegex(BuildError, "port mapping"):
                validate_port_defaults(mapping)

    @unittest.skipUnless(os.name == "nt", "The portable build requires Windows")
    def test_build_rejects_invalid_mapping_before_dependency_or_build_commands(self) -> None:
        from build import main

        with tempfile.TemporaryDirectory() as directory:
            mapping = Path(directory) / "port_mapping.json"
            mapping.write_text("{}", encoding="utf-8")
            with (
                mock.patch("build.PORT_MAPPING", mapping),
                mock.patch("build.select_python") as python,
                mock.patch("build.run") as run,
            ):
                with self.assertRaisesRegex(BuildError, "port mapping"):
                    main(["--skip-install"])
                python.assert_not_called()
                run.assert_not_called()

    def test_validation_rejects_an_incorrect_runtime_default(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packages = Path(directory)
            write_packages(packages, "new")
            with self.assertRaisesRegex(BuildError, "Unexpected runtime defaults"):
                validate_artifacts(packages / "Debug", host="0.0.0.0")

    def test_validation_rejects_invalid_routing_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            packages = Path(directory)
            write_packages(packages, "new")
            for configuration in ("{", "[]"):
                with self.subTest(configuration=configuration):
                    (packages / "Debug" / INITIAL_CONFIGURATION).write_text(configuration, encoding="utf-8")
                    with self.assertRaisesRegex(BuildError, "routing configuration"):
                        validate_artifacts(packages / "Debug", host=PROFILES["Debug"])

    def test_freezer_collects_lazy_sdk_resources_and_uvicorn_backends(self) -> None:
        command = [str(value) for value in pyinstaller_command(Path("python.exe"), Path("stage"))]
        submodules = [command[index + 1] for index, value in enumerate(command)
                      if value == "--collect-submodules"]
        self.assertEqual(set(submodules), {"openai", "anthropic"})
        hidden = [command[index + 1] for index, value in enumerate(command)
                  if value == "--hidden-import"]
        self.assertIn("uvicorn.protocols.http.h11_impl", hidden)
        self.assertIn("backend.app.main", hidden)
        self.assertIn("google.genai", hidden)
        self.assertIn("--additional-hooks-dir", command)


class PublicationTests(unittest.TestCase):
    def test_first_install_publishes_routing_configuration_for_both_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            stage = repository / "build" / "packages"
            write_packages(stage, "new")
            expected = managed_contents(stage)
            publish_artifacts(stage, repository=repository)
            self.assertEqual(managed_contents(repository / "dist"), expected)

    def test_replacement_preserves_each_modes_database_and_local_configuration(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            output = repository / "dist"
            stage = repository / "build" / "packages"
            write_packages(output, "old")
            write_packages(stage, "new")
            old_artifacts = managed_contents(output)
            new_artifacts = managed_contents(stage)
            user_state = {}
            for profile in PROFILES:
                package = output / profile
                (package / "data").mkdir()
                (package / "data" / "counter.db").write_bytes(f"{profile} counter data".encode("ascii"))
                (package / ".env").write_text(f"BACKEND_PORT=9000\nMODE={profile}\n", encoding="utf-8")
                (package / "notes.txt").write_text(f"{profile} user notes", encoding="utf-8")
                user_state[profile] = {
                    name: (package / name).read_bytes()
                    for name in ("data/counter.db", ".env", "notes.txt")
                }

            publish_artifacts(stage, repository=repository)

            for profile in PROFILES:
                with self.subTest(profile=profile):
                    actual = package_contents(output / profile)
                    self.assertEqual(
                        {path: content for path, content in actual.items() if path.parts[0] in ARTIFACTS},
                        {path: content for path, content in new_artifacts[profile].items() if path.parts[0] in ARTIFACTS},
                    )
                    for name, content in user_state[profile].items():
                        self.assertEqual((output / profile / name).read_bytes(), content)
                    self.assertEqual(
                        package_contents(stage / "previous" / profile),
                        {path: content for path, content in old_artifacts[profile].items() if path.parts[0] in ARTIFACTS},
                    )
                    self.assertEqual(json.loads((output / profile / INITIAL_CONFIGURATION).read_text()), {"version": "old"})
                    self.assertFalse(any((stage / profile / name).exists() for name in ARTIFACTS))

    def test_locked_release_destination_rolls_back_both_existing_modes(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            output = repository / "dist"
            stage = repository / "build" / "packages"
            write_packages(output, "old")
            write_packages(stage, "new")
            for profile in PROFILES:
                (output / profile / ".env").write_text(f"MODE={profile}", encoding="utf-8")
            old_contents = managed_contents(output)
            new_contents = managed_contents(stage)
            original_rename = Path.rename
            moves = []

            def fail_locked_release(source: Path, target: Path) -> Path:
                if source == output / "Release" / "public":
                    raise OSError("simulated locked Release destination")
                moves.append((source, Path(target)))
                return original_rename(source, target)

            with mock.patch.object(Path, "rename", new=fail_locked_release):
                with self.assertRaisesRegex(BuildError, "Could not replace Debug/Release artifacts"):
                    publish_artifacts(stage, repository=repository)

            for name in ARTIFACTS:
                self.assertIn((stage / "Debug" / name, output / "Debug" / name), moves)
            self.assertIn((stage / "Release" / "server.exe", output / "Release" / "server.exe"), moves)
            self.assertEqual(managed_contents(output), old_contents)
            self.assertEqual(managed_contents(stage), new_contents)

    def test_failed_first_install_leaves_no_mixed_artifacts(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            output = repository / "dist"
            stage = repository / "build" / "packages"
            write_packages(stage, "new")
            new_contents = managed_contents(stage)
            original_rename = Path.rename

            def fail_release_install(source: Path, target: Path) -> Path:
                if source == stage / "Release" / "public":
                    raise OSError("simulated failed first installation")
                return original_rename(source, target)

            with mock.patch.object(Path, "rename", new=fail_release_install):
                with self.assertRaises(BuildError):
                    publish_artifacts(stage, repository=repository)

            self.assertEqual(managed_contents(stage), new_contents)
            for profile in PROFILES:
                with self.subTest(profile=profile):
                    self.assertEqual(package_contents(output / profile), {})
                    self.assertFalse(any((output / profile / name).exists() for name in ARTIFACTS))

    def test_unsafe_release_target_is_rejected_before_either_mode_is_moved(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            output = repository / "dist"
            stage = repository / "build" / "packages"
            write_packages(output, "old")
            write_packages(stage, "new")
            old_contents = managed_contents(output)
            new_contents = managed_contents(stage)
            original_resolve = Path.resolve

            def resolve_unsafe_target(path: Path, *args: object, **kwargs: object) -> Path:
                if path == output / "Release" / "public":
                    return repository / "outside"
                return original_resolve(path, *args, **kwargs)

            with (
                mock.patch.object(Path, "resolve", new=resolve_unsafe_target),
                mock.patch.object(Path, "rename") as rename,
            ):
                with self.assertRaisesRegex(BuildError, "Unsafe build path"):
                    publish_artifacts(stage, repository=repository)
                rename.assert_not_called()

            self.assertEqual(managed_contents(output), old_contents)
            self.assertEqual(managed_contents(stage), new_contents)

    def test_checked_path_rejects_parent_and_paths_escaping_the_workspace(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory) / "repository"
            repository.mkdir()
            for unsafe in (repository, repository / ".." / "outside"):
                with self.subTest(path=unsafe), self.assertRaises(BuildError):
                    checked_path(unsafe, repository)


class FrozenStartupTests(unittest.TestCase):
    @staticmethod
    def run_command(command, **kwargs):
        if "--initialize-settings" in command:
            from backend.app.user_settings import initialize_port_settings

            path = Path(kwargs["env"]["CONNECTOR_SETTINGS_PATH"])
            initialize_port_settings(path, defaults_path=Path(kwargs["cwd"]) / "port_mapping.json")
        return mock.Mock(returncode=0, stdout=b"")

    def test_startup_failure_reports_child_log_and_isolates_user_state(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            package = stage / "packages" / "Debug"
            package.mkdir(parents=True)
            (package / "server.exe").write_bytes(b"frozen stub")
            (package / "run.bat").write_text(LAUNCHER, encoding="ascii")
            (package / "port_mapping.json").write_text(
                '{"react_port": 5300, "connector_port": 8300, "kokoro_port": 8301}', encoding="utf-8",
            )
            process = mock.Mock(returncode=1)
            process.poll.return_value = 1

            def fail_startup(*args, **kwargs):
                kwargs["stdout"].write(b"Missing frozen dependency")
                return process

            with (
                mock.patch("build.subprocess.Popen", side_effect=fail_startup) as start,
                mock.patch("build.subprocess.run", side_effect=self.run_command),
            ):
                with self.assertRaisesRegex(BuildError, "Missing frozen dependency"):
                    smoke_test(package, stage / "smoke")
            environment = start.call_args.kwargs["env"]
            self.assertEqual(environment["CONNECTOR_SETTINGS_PATH"], str(stage / "smoke" / "settings" / "settings.json"))
            self.assertEqual(environment["DATA_DIR"], str(stage / "smoke" / "data"))
            process.terminate.assert_not_called()

    def test_successful_smoke_stops_child_process(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            package = stage / "packages" / "Debug"
            package.mkdir(parents=True)
            (package / "server.exe").write_bytes(b"frozen stub")
            (package / "run.bat").write_text(LAUNCHER, encoding="ascii")
            (package / "port_mapping.json").write_text(
                '{"react_port": 5300, "connector_port": 8300, "kokoro_port": 8301}', encoding="utf-8",
            )
            process = mock.Mock(pid=12345)
            process.poll.return_value = None
            success = mock.MagicMock()
            success.__enter__.return_value.status = 200
            proxy = HTTPError("http://localhost/v1/check", 404, "Not found", {},
                              io.BytesIO(b'{"detail": "Not Found"}'))
            settings = mock.MagicMock()
            settings.__enter__.return_value.read.return_value = json.dumps({
                "data_dir": str(stage / "smoke" / "data"),
                "settings_path": str(stage / "smoke" / "settings" / "settings.json"),
            }).encode("utf-8")
            opener = mock.Mock()
            opener.open.side_effect = [success, success, settings, proxy]
            with (
                mock.patch("build.subprocess.Popen", return_value=process),
                mock.patch("build.subprocess.run", side_effect=self.run_command) as taskkill,
                mock.patch("build.build_opener", return_value=opener),
            ):
                smoke_test(package, stage / "smoke")
            if os.name == "nt":
                self.assertEqual(taskkill.call_args.args[0][1:], ["/PID", "12345", "/T", "/F"])
                process.terminate.assert_not_called()
            else:
                process.terminate.assert_called_once()
            process.wait.assert_called_once()


if __name__ == "__main__":
    unittest.main()
