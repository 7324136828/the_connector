from __future__ import annotations

import json
import io
import os
import tempfile
import unittest
import wave
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
    kokoro_bundle_device,
    kokoro_pyinstaller_command,
    prepare_artifacts,
    publish_artifacts,
    pyinstaller_command,
    smoke_test,
    select_kokoro_python,
    smoke_test_cuda_on_cpu,
    validate_artifacts,
    validate_kokoro_bundle,
    validate_kokoro_health,
    validate_port_defaults,
)


def write_kokoro_bundle(bundle: Path, version: str = "new", *, device: str = "cpu") -> None:
    (bundle / "_internal" / "torch" / "lib").mkdir(parents=True)
    (bundle / "kokoro.exe").write_bytes(f"kokoro-{version}".encode("ascii"))
    (bundle / "_internal" / "python312.dll").write_bytes(f"python-{version}".encode("ascii"))
    (bundle / "_internal" / "torch" / "lib" / "torch_cpu.dll").write_bytes(f"torch-{version}".encode("ascii"))
    (bundle / "runtime.json").write_text(json.dumps({"device": device}), encoding="ascii")
    if device == "cuda":
        for name in ("torch_cuda.dll", "cudart64_12.dll"):
            (bundle / "_internal" / "torch" / "lib" / name).write_bytes(f"cuda-{version}".encode("ascii"))
    (bundle / "_internal" / "language_tags" / "data" / "json").mkdir(parents=True)
    (bundle / "_internal" / "language_tags" / "data" / "json" / "index.json").write_bytes(b"{}")
    (bundle / "_internal" / "kokoro-model" / "voices").mkdir(parents=True)
    for name in ("config.json", "kokoro-v1_0.pth", "voices/af_heart.pt"):
        (bundle / "_internal" / "kokoro-model" / name).write_bytes(f"asset-{version}".encode("ascii"))


def write_packages(directory: Path, version: str) -> None:
    for profile, host in PROFILES.items():
        package = directory / profile
        package.mkdir(parents=True, exist_ok=True)
        (package / "server.exe").write_bytes(f"server-{profile}-{version}".encode("ascii"))
        write_kokoro_bundle(package / "python-kokoro", f"{profile}-{version}")
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
            write_kokoro_bundle(stage / "python-kokoro", device="cuda")
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
                    self.assertEqual(package_contents(package / "python-kokoro"), package_contents(stage / "python-kokoro"))
                    self.assertEqual(kokoro_bundle_device(package / "python-kokoro"), "cuda")
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
            (packages / "Debug" / "python-kokoro" / "_internal" / "python312.dll").write_bytes(b"changed")
            self.assertEqual((packages / "Release" / "server.exe").read_bytes(), b"shared backend")
            self.assertEqual(
                (packages / "Release" / "public" / "index.html").read_text(encoding="utf-8"),
                "shared frontend",
            )
            self.assertEqual((packages / "Release" / "python-kokoro" / "_internal" / "python312.dll").read_bytes(), b"python-new")

    def test_validation_requires_the_kokoro_executable_native_runtime_and_model(self) -> None:
        for name in ("runtime.json", "kokoro.exe", "_internal/python312.dll", "_internal/torch/lib/torch_cpu.dll",
                     "_internal/language_tags/data/json/index.json",
                     "_internal/kokoro-model/config.json", "_internal/kokoro-model/kokoro-v1_0.pth",
                     "_internal/kokoro-model/voices/af_heart.pt"):
            with self.subTest(missing=name), tempfile.TemporaryDirectory() as directory:
                bundle = Path(directory) / "python-kokoro"
                write_kokoro_bundle(bundle)
                (bundle / name).unlink()
                with self.assertRaisesRegex(BuildError, "Kokoro"):
                    validate_kokoro_bundle(bundle)

    def test_cuda_bundle_requires_cuda_native_libraries(self) -> None:
        for name in ("torch_cuda.dll", "cudart64_12.dll"):
            with self.subTest(missing=name), tempfile.TemporaryDirectory() as directory:
                bundle = Path(directory) / "python-kokoro"
                write_kokoro_bundle(bundle, device="cuda")
                validate_kokoro_bundle(bundle)
                (bundle / "_internal" / "torch" / "lib" / name).unlink()
                with self.assertRaisesRegex(BuildError, "CUDA PyTorch runtime"):
                    validate_kokoro_bundle(bundle)

    def test_bundle_device_rejects_missing_invalid_or_auto_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            bundle = Path(directory) / "python-kokoro"
            write_kokoro_bundle(bundle)
            for content in ("{", "[]", "{}", '{"device": "auto"}', '{"device": []}'):
                with self.subTest(content=content):
                    (bundle / "runtime.json").write_text(content, encoding="ascii")
                    with self.assertRaisesRegex(BuildError, "Kokoro runtime"):
                        kokoro_bundle_device(bundle)

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

    def test_kokoro_freezer_is_a_separate_directory_with_offline_assets_and_metadata(self) -> None:
        command = [str(value) for value in kokoro_pyinstaller_command(Path("kokoro-python.exe"), Path("stage"))]
        self.assertEqual(command[0], "kokoro-python.exe")
        self.assertIn("--onedir", command)
        self.assertNotIn("--onefile", command)
        self.assertIn(f"{Path('stage') / 'kokoro-model'}{os.pathsep}kokoro-model", command)
        collected = [command[index + 1] for index, value in enumerate(command) if value == "--collect-all"]
        self.assertEqual(set(collected), {"kokoro", "misaki", "en_core_web_sm", "espeakng_loader"})
        data = [command[index + 1] for index, value in enumerate(command) if value == "--collect-data"]
        self.assertEqual(set(data), {"language_tags", "phonemizer"})
        metadata = [command[index + 1] for index, value in enumerate(command) if value == "--copy-metadata"]
        self.assertIn("en-core-web-sm", metadata)
        self.assertIn("kokoro", metadata)
        self.assertIn("phonemizer-fork", metadata)
        hidden = [command[index + 1] for index, value in enumerate(command) if value == "--hidden-import"]
        self.assertIn("transformers.models.albert.configuration_albert", hidden)
        self.assertIn("transformers.models.albert.modeling_albert", hidden)
        self.assertTrue(command[-1].endswith(str(Path("python-kokoro") / "server.py")))

    def test_skip_install_requires_a_prepared_isolated_kokoro_environment(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            # Match checked_path's expansion of Windows short-path aliases.
            work = Path(directory).resolve()
            with mock.patch("build.WORK", work), mock.patch("build.run") as run:
                with self.assertRaisesRegex(BuildError, "Prepared Kokoro build environment is missing"):
                    select_kokoro_python("cpu", skip_install=True)
                run.assert_not_called()
                python = work / "kokoro-venv-cpu" / "Scripts" / "python.exe"
                python.parent.mkdir(parents=True)
                python.write_bytes(b"python")
                self.assertEqual(select_kokoro_python("cpu", skip_install=True), python)
                self.assertEqual(run.call_count, 2)
                self.assertTrue(all(call.args[0][0] == python for call in run.call_args_list))
                self.assertFalse(any("pip" in call.args[0] for call in run.call_args_list))

    def test_cuda_build_preflight_executes_a_gpu_tensor(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            python = work / "kokoro-venv-cuda" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"python")
            with mock.patch("build.WORK", work), mock.patch("build.run") as run:
                self.assertEqual(select_kokoro_python("cuda", skip_install=True), python)
            preflight = run.call_args.args[0]
            self.assertEqual(preflight[:2], [python, "-c"])
            self.assertIn("torch.cuda.is_available()", preflight[2])
            self.assertIn("torch.ones(1, device='cuda').add_(1).item()", preflight[2])

    def test_cuda_cpu_validation_still_requires_cuda_wheels_without_probing_a_gpu(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory).resolve()
            python = work / "kokoro-venv-cuda" / "Scripts" / "python.exe"
            python.parent.mkdir(parents=True)
            python.write_bytes(b"python")
            with mock.patch("build.WORK", work), mock.patch("build.run") as run:
                self.assertEqual(
                    select_kokoro_python("cuda", skip_install=True, require_gpu=False), python,
                )
            checks = [call.args[0][2] for call in run.call_args_list]
            self.assertTrue(any("bool(torch.version.cuda) == True" in check for check in checks))
            self.assertFalse(any("torch.cuda.is_available()" in check or "device='cuda'" in check
                                 for check in checks))

    @unittest.skipUnless(os.name == "nt", "The portable build requires Windows")
    def test_build_defaults_to_cuda_and_cpu_is_explicit(self) -> None:
        from build import main

        for arguments, expected, require_gpu in (
            (["--skip-install"], "cuda", True),
            (["--skip-install", "--kokoro-device", "cpu"], "cpu", True),
            (["--skip-install", "--kokoro-device", "cuda", "--cuda-smoke-device", "cpu"], "cuda", False),
        ):
            with self.subTest(device=expected), tempfile.TemporaryDirectory() as directory:
                with (
                    mock.patch("build.ROOT", Path(directory)),
                    mock.patch("build.WORK", Path(directory) / "build"),
                    mock.patch("build.select_python", return_value=Path("python.exe")),
                    mock.patch("build.shutil.which", return_value="npm.cmd"),
                    mock.patch("build.select_kokoro_python", side_effect=BuildError("test stop")) as select,
                ):
                    with self.assertRaisesRegex(BuildError, "test stop"):
                        main(arguments)
                    select.assert_called_once_with(expected, skip_install=True, require_gpu=require_gpu)


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

    def test_locked_kokoro_bundle_rolls_back_both_profiles(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            repository = Path(directory)
            output = repository / "dist"
            stage = repository / "build" / "packages"
            write_packages(output, "old")
            write_packages(stage, "new")
            old_contents = managed_contents(output)
            new_contents = managed_contents(stage)
            original_rename = Path.rename

            def fail_locked_kokoro(source: Path, target: Path) -> Path:
                if source == output / "Release" / "python-kokoro":
                    raise OSError("simulated running Kokoro")
                return original_rename(source, target)

            with mock.patch.object(Path, "rename", new=fail_locked_kokoro):
                with self.assertRaisesRegex(BuildError, "Could not replace Debug/Release artifacts"):
                    publish_artifacts(stage, repository=repository)
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
    def test_cuda_cpu_smoke_restores_the_exact_cuda_profile_on_success_and_failure(self) -> None:
        for fail in (False, True):
            with self.subTest(fail=fail), tempfile.TemporaryDirectory() as directory:
                stage = Path(directory)
                package = stage / "packages" / "Debug"
                bundle = package / "python-kokoro"
                write_kokoro_bundle(bundle, device="cuda")
                profile = bundle / "runtime.json"
                original = b'\xef\xbb\xbf{ "device": "cuda", "preserved": true }\r\n'
                profile.write_bytes(original)

                def check_cpu_override(actual_package, workspace):
                    self.assertEqual(actual_package, package)
                    self.assertEqual(workspace, stage / "smoke")
                    self.assertEqual(kokoro_bundle_device(bundle), "cpu")
                    self.assertTrue((bundle / "_internal" / "torch" / "lib" / "torch_cuda.dll").is_file())
                    if fail:
                        raise BuildError("speech failed")

                with mock.patch("build.smoke_test", side_effect=check_cpu_override) as smoke:
                    if fail:
                        with self.assertRaisesRegex(BuildError, "speech failed"):
                            smoke_test_cuda_on_cpu(package, stage / "smoke")
                    else:
                        smoke_test_cuda_on_cpu(package, stage / "smoke")
                    smoke.assert_called_once()
                self.assertEqual(profile.read_bytes(), original)
                validate_kokoro_bundle(bundle)

    def test_cuda_cpu_smoke_rejects_a_cpu_only_bundle(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            package = Path(directory) / "Debug"
            write_kokoro_bundle(package / "python-kokoro", device="cpu")
            with mock.patch("build.smoke_test") as smoke:
                with self.assertRaisesRegex(BuildError, "requires a CUDA bundle"):
                    smoke_test_cuda_on_cpu(package, Path(directory) / "smoke")
                smoke.assert_not_called()

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
            write_kokoro_bundle(package / "python-kokoro")
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

    def run_successful_smoke(self, device: str, actual_device: str) -> None:
        with tempfile.TemporaryDirectory() as directory:
            stage = Path(directory)
            package = stage / "packages" / "Debug"
            package.mkdir(parents=True)
            (package / "server.exe").write_bytes(b"frozen stub")
            write_kokoro_bundle(package / "python-kokoro", device=device)
            (package / "run.bat").write_text(LAUNCHER, encoding="ascii")
            (package / "port_mapping.json").write_text(
                '{"react_port": 5300, "connector_port": 8300, "kokoro_port": 8301}', encoding="utf-8",
            )
            process = mock.Mock(pid=12345)
            process.poll.return_value = None
            success = mock.MagicMock()
            success.__enter__.return_value.status = 200
            health = mock.MagicMock()
            health.__enter__.return_value.status = 200
            health.__enter__.return_value.read.return_value = b'{"status": "ok"}'
            speech_health = mock.MagicMock()
            speech_health.__enter__.return_value.status = 200
            speech_health.__enter__.return_value.read.return_value = json.dumps({
                "status": "ok", "service": "python-kokoro", "device": device,
            }).encode("utf-8")
            loaded_health = mock.MagicMock()
            loaded_health.__enter__.return_value.status = 200
            loaded_health.__enter__.return_value.read.return_value = json.dumps({
                "status": "ok", "device": device, "modelDevices": {"a": actual_device},
            }).encode("utf-8")
            voices = mock.MagicMock()
            voices.__enter__.return_value.status = 200
            voices.__enter__.return_value.read.return_value = b'{"voices": ["af_heart"]}'
            audio = io.BytesIO()
            with wave.open(audio, "wb") as wav:
                wav.setnchannels(1)
                wav.setsampwidth(2)
                wav.setframerate(24000)
                wav.writeframes(b"\x00\x00" * 100)
            speech = mock.MagicMock()
            speech.__enter__.return_value.status = 200
            speech.__enter__.return_value.read.return_value = audio.getvalue()
            proxy = HTTPError("http://localhost/v1/check", 404, "Not found", {},
                              io.BytesIO(b'{"detail": "Not Found"}'))
            settings = mock.MagicMock()
            settings.__enter__.return_value.read.return_value = json.dumps({
                "data_dir": str(stage / "smoke" / "data"),
                "settings_path": str(stage / "smoke" / "settings" / "settings.json"),
            }).encode("utf-8")
            opener = mock.Mock()
            opener.open.side_effect = [health, success, speech_health, voices, settings, proxy, speech, loaded_health]
            with (
                mock.patch("build.subprocess.Popen", return_value=process) as start,
                mock.patch("build.subprocess.run", side_effect=self.run_command) as taskkill,
                mock.patch("build.build_opener", return_value=opener),
                mock.patch.dict(os.environ, {
                    "KOKORO_DEVICE": "cpu", "PODCAST_DEVICE": "cpu",
                    "HF_HUB_CACHE": str(stage / "shared-huggingface-cache"),
                }),
            ):
                smoke_test(package, stage / "smoke")
            environment = start.call_args.kwargs["env"]
            saved = json.loads((stage / "smoke" / "settings" / "settings.json").read_text())
            ports = [saved[name] for name in ("react_port", "connector_port", "kokoro_port")]
            self.assertEqual(len(set(ports)), 3)
            self.assertNotIn("KOKORO_BASE_URL", environment)
            self.assertNotIn("KOKORO_DEVICE", environment)
            self.assertNotIn("PODCAST_DEVICE", environment)
            self.assertEqual(environment["HF_HUB_CACHE"], str(stage / "smoke" / "huggingface" / "hub"))
            self.assertFalse(Path(environment["HF_HUB_CACHE"]).exists())
            self.assertEqual(environment["HF_HUB_OFFLINE"], "1")
            urls = [getattr(call.args[0], "full_url", call.args[0]) for call in opener.open.call_args_list]
            self.assertIn(f"http://127.0.0.1:{saved['react_port']}/api/speech/health", urls)
            self.assertIn(f"http://127.0.0.1:{saved['react_port']}/api/speech/voices", urls)
            self.assertIn(f"http://127.0.0.1:{saved['react_port']}/api/speech", urls)
            if os.name == "nt":
                self.assertEqual(taskkill.call_args.args[0][1:], ["/PID", "12345", "/T", "/F"])
                process.terminate.assert_not_called()
            else:
                process.terminate.assert_called_once()
            process.wait.assert_called_once()

    def test_cpu_smoke_checks_loaded_model_and_stops_child_process(self) -> None:
        self.run_successful_smoke("cpu", "cpu")

    def test_cuda_smoke_checks_loaded_gpu_model_and_stops_child_process(self) -> None:
        self.run_successful_smoke("cuda", "cuda:0")

    def test_cuda_smoke_rejects_cpu_model_after_successful_speech(self) -> None:
        with self.assertRaisesRegex(BuildError, "model used unexpected devices"):
            self.run_successful_smoke("cuda", "cpu")

    def test_health_validation_checks_actual_devices_after_synthesis(self) -> None:
        for expected, health in (
            ("cuda", {"device": "cpu"}),
            ("cuda", {"device": "cuda", "modelDevices": {}}),
            ("cuda", {"device": "cuda", "modelDevices": {"a": "cuda:0", "b": "cpu"}}),
            ("cpu", {"device": "cpu", "modelDevices": {"a": "cuda:0"}}),
            ("cpu", {"device": "cpu", "modelDevices": {"a": None}}),
        ):
            with self.subTest(expected=expected, health=health), self.assertRaises(BuildError):
                validate_kokoro_health(health, expected, model_loaded=True)
        validate_kokoro_health({"device": "cuda", "modelDevices": {"a": "cuda:1"}},
                              "cuda", model_loaded=True)


if __name__ == "__main__":
    unittest.main()
