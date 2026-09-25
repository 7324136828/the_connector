"""Contract tests for the isolated Kokoro HTTP service."""

from __future__ import annotations

import json
import sys
import threading
import unittest
from http.server import ThreadingHTTPServer
from types import SimpleNamespace
from unittest.mock import Mock, patch
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import server


class KokoroDeviceTest(unittest.TestCase):
    def fake_torch(self, available=True, kernel_error=None):
        torch = SimpleNamespace(
            cuda=SimpleNamespace(
                is_available=Mock(return_value=available),
                get_device_name=Mock(return_value="Test CUDA GPU"),
            ),
            version=SimpleNamespace(cuda="12.8"),
            ones=Mock(side_effect=kernel_error),
        )
        return torch

    def test_auto_checks_a_real_cuda_operation_before_selecting_gpu(self):
        torch = self.fake_torch()
        with patch.dict(sys.modules, {"torch": torch}):
            self.assertEqual(server.select_device("auto"), "cuda")
        torch.ones.assert_called_once_with(1, device="cuda")
        torch.ones.return_value.add_.assert_called_once_with(1)
        torch.ones.return_value.add_.return_value.item.assert_called_once_with()

    def test_explicit_cpu_never_probes_cuda(self):
        torch = self.fake_torch()
        with patch.dict(sys.modules, {"torch": torch}):
            self.assertEqual(server.select_device("cpu"), "cpu")
        torch.cuda.is_available.assert_not_called()
        torch.ones.assert_not_called()

    def test_unavailable_cuda_fails_when_required_and_falls_back_for_auto(self):
        torch = self.fake_torch(available=False)
        with patch.dict(sys.modules, {"torch": torch}):
            with self.assertRaisesRegex(RuntimeError, "CUDA was requested"):
                server.select_device("cuda")
            with self.assertLogs(server.LOG, level="WARNING"):
                self.assertEqual(server.select_device("auto"), "cpu")
        torch.ones.assert_not_called()

    def test_incompatible_cuda_kernel_cannot_silently_fall_back_when_required(self):
        torch = self.fake_torch(kernel_error=RuntimeError("no kernel image available"))
        with patch.dict(sys.modules, {"torch": torch}):
            with self.assertRaisesRegex(RuntimeError, "no kernel image available"):
                server.select_device("cuda")
            with self.assertLogs(server.LOG, level="WARNING"):
                self.assertEqual(server.select_device("auto"), "cpu")

    def test_unsupported_device_is_rejected(self):
        with self.assertRaisesRegex(ValueError, "Unsupported device"):
            server.select_device("invalid")

    def test_pipeline_reuses_selected_gpu_and_health_reports_actual_model_device(self):
        torch = self.fake_torch()
        pipeline = Mock()
        pipeline.model.parameters.side_effect = lambda: iter(
            [SimpleNamespace(device="cuda:0")]
        )
        with patch.dict(sys.modules, {"torch": torch}), patch.object(
            server.kokoro, "KPipeline", return_value=pipeline
        ) as make_pipeline:
            service = server.KokoroService("cuda")
            unloaded = service.health()
            self.assertFalse(unloaded["modelLoaded"])
            self.assertEqual(unloaded["modelDevices"], {})
            self.assertIs(service.pipeline("a"), pipeline)
            self.assertIs(service.pipeline("a"), pipeline)
            health = service.health()

        make_pipeline.assert_called_once_with(
            lang_code="a", repo_id=server.MODEL_REPO, device="cuda"
        )
        self.assertEqual(health["pythonExecutable"], sys.executable)
        self.assertEqual(health["pythonPrefix"], sys.prefix)
        self.assertEqual(health["requestedDevice"], "cuda")
        self.assertEqual(health["device"], "cuda")
        self.assertEqual(health["modelDevices"], {"a": "cuda:0"})
        self.assertEqual(health["gpuName"], "Test CUDA GPU")
        self.assertEqual(health["cudaVersion"], "12.8")
        self.assertTrue(health["cudaAvailable"])
        self.assertTrue(health["modelLoaded"])

    def test_device_environment_uses_kokoro_specific_setting(self):
        with patch.dict(server.os.environ, {"KOKORO_DEVICE": "cuda", "PODCAST_DEVICE": "cpu"}):
            self.assertEqual(server.parse_args([]).device, "cuda")
            self.assertEqual(server.parse_args(["--device", "cpu"]).device, "cpu")

    def test_default_port_is_the_isolated_backend_port(self):
        self.assertEqual(server.parse_args([]).port, 8302)


class FakeService:
    def health(self):
        return {
            "status": "ok",
            "service": "python-kokoro",
            "python": "3.12.0",
            "device": "cuda",
        }

    def synthesize(self, text, voice, speed, lang_code):
        self.last_request = (text, voice, speed, lang_code)
        return b"RIFF-test-wave", {
            "X-Audio-Duration": "1.0",
            "X-Render-Seconds": "0.1",
            "X-Real-Time-Factor": "0.1",
        }


class KokoroServerTest(unittest.TestCase):
    def setUp(self) -> None:
        self.service = FakeService()
        self.httpd = ThreadingHTTPServer(("127.0.0.1", 0), server.KokoroRequestHandler)
        self.httpd.kokoro_service = self.service
        self.thread = threading.Thread(target=self.httpd.serve_forever, daemon=True)
        self.thread.start()
        self.base = f"http://127.0.0.1:{self.httpd.server_port}"

    def tearDown(self) -> None:
        self.httpd.shutdown()
        self.httpd.server_close()
        self.thread.join(timeout=2)

    def test_health_identifies_the_isolated_service(self) -> None:
        with urlopen(f"{self.base}/health") as response:
            body = json.loads(response.read())
        self.assertEqual(response.status, 200)
        self.assertEqual(body["service"], "python-kokoro")
        self.assertEqual(body["device"], "cuda")

    def test_openai_compatible_speech_route_returns_wav(self) -> None:
        payload = json.dumps(
            {
                "model": "kokoro",
                "input": "Hello.",
                "voice": "af_heart",
                "speed": 0.96,
                "response_format": "wav",
                "language": "a",
            }
        ).encode("utf-8")
        request = Request(
            f"{self.base}/v1/audio/speech",
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with urlopen(request) as response:
            body = response.read()

        self.assertEqual(response.status, 200)
        self.assertEqual(response.headers.get_content_type(), "audio/wav")
        self.assertEqual(response.headers["X-Real-Time-Factor"], "0.1")
        self.assertEqual(body, b"RIFF-test-wave")
        self.assertEqual(
            self.service.last_request,
            ("Hello.", "af_heart", 0.96, "a"),
        )

    def test_speech_route_rejects_non_wav_format(self) -> None:
        request = Request(
            f"{self.base}/v1/audio/speech",
            data=json.dumps(
                {"input": "Hello.", "voice": "af_heart", "response_format": "mp3"}
            ).encode("utf-8"),
            method="POST",
            headers={"Content-Type": "application/json"},
        )
        with self.assertRaises(HTTPError) as raised:
            urlopen(request)
        self.assertEqual(raised.exception.code, 400)


if __name__ == "__main__":
    unittest.main()
