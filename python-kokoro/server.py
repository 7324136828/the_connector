"""Isolated Python 3.12 service for the official Kokoro PyTorch package.

The API deliberately follows Kokoro-FastAPI's stable OpenAI-compatible speech
route.  The main application never imports ``kokoro``; it exchanges WAV audio
with this process over loopback HTTP instead.
"""

from __future__ import annotations

import argparse
import io
import json
import logging
import os
import sys
import threading
import time
import wave
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from importlib.metadata import PackageNotFoundError, version
from typing import Any
from urllib.parse import urlsplit

import kokoro


MODEL_REPO = "hexgrad/Kokoro-82M"
SAMPLE_RATE = 24_000
MAX_REQUEST_BYTES = 2 * 1024 * 1024
LOG = logging.getLogger("kokoro-service")


def package_version(name: str) -> str:
    try:
        return version(name)
    except PackageNotFoundError:
        return "not-installed"


def select_device(requested: str) -> str:
    if requested not in {"auto", "cpu", "cuda"}:
        raise ValueError(f"Unsupported device: {requested}")
    if requested == "cpu":
        return "cpu"

    import torch

    try:
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA is not available to PyTorch")
        torch.ones(1, device="cuda").add_(1).item()
        return "cuda"
    except (RuntimeError, AssertionError) as error:
        if requested == "cuda":
            raise RuntimeError(f"CUDA was requested but could not start: {error}") from error
        LOG.warning("CUDA is unavailable; using CPU: %s", error)
        return "cpu"


def wav_bytes(audio: Any, sample_rate: int = SAMPLE_RATE) -> bytes:
    import numpy as np

    samples = np.asarray(audio, dtype=np.float32).reshape(-1)
    if not np.isfinite(samples).all():
        raise RuntimeError("Kokoro returned non-finite audio samples")
    pcm = (np.clip(samples, -1.0, 1.0) * 32767).astype("<i2")
    output = io.BytesIO()
    with wave.open(output, "wb") as target:
        target.setnchannels(1)
        target.setsampwidth(2)
        target.setframerate(sample_rate)
        target.writeframes(pcm.tobytes())
    return output.getvalue()


class KokoroService:
    """Own the model and serialize requests through one reusable pipeline."""

    def __init__(self, requested_device: str = "auto") -> None:
        self.requested_device = requested_device
        self.device = select_device(requested_device)
        self._pipelines: dict[str, Any] = {}
        self._pipeline_lock = threading.Lock()
        self._synthesis_lock = threading.Lock()

    def pipeline(self, lang_code: str):
        with self._pipeline_lock:
            pipeline = self._pipelines.get(lang_code)
            if pipeline is None:
                LOG.info(
                    "Loading official Kokoro pipeline: language=%s device=%s",
                    lang_code,
                    self.device,
                )
                pipeline = kokoro.KPipeline(
                    lang_code=lang_code,
                    repo_id=MODEL_REPO,
                    device=self.device,
                )
                self._pipelines[lang_code] = pipeline
            return pipeline

    def synthesize(
        self, text: str, voice: str, speed: float, lang_code: str
    ) -> tuple[bytes, dict[str, str]]:
        import numpy as np

        started = time.perf_counter()
        # KPipeline and its phonemizer keep shared state. A single service-level
        # lock is conservative and makes concurrent HTTP requests deterministic.
        with self._synthesis_lock:
            chunks = []
            for _graphemes, _phonemes, audio in self.pipeline(lang_code)(
                text, voice=voice, speed=speed
            ):
                if audio is not None:
                    if hasattr(audio, "detach"):
                        audio = audio.detach().cpu().numpy()
                    chunks.append(np.asarray(audio, dtype=np.float32).reshape(-1))
        if not chunks:
            raise RuntimeError("Kokoro returned no audio")
        combined = np.concatenate(chunks)
        elapsed = time.perf_counter() - started
        duration = combined.size / SAMPLE_RATE
        LOG.info(
            "Synthesized %d chars into %.2fs audio in %.2fs (RTF %.3f)",
            len(text),
            duration,
            elapsed,
            elapsed / duration,
        )
        return wav_bytes(combined), {
            "X-Audio-Duration": f"{duration:.6f}",
            "X-Render-Seconds": f"{elapsed:.6f}",
            "X-Real-Time-Factor": f"{elapsed / duration:.6f}",
        }

    def health(self) -> dict[str, Any]:
        import torch

        kokoro_version = package_version("kokoro")
        torch_version = package_version("torch")
        # Report the tensors' actual devices as well as the requested setting.
        # A pipeline is published only after it has finished loading, so health
        # probes need not wait for the model-loading lock.
        pipelines = self._pipelines.copy()
        model_devices = {}
        for language, pipeline in pipelines.items():
            parameter = next(pipeline.model.parameters(), None)
            model_devices[language] = str(parameter.device) if parameter is not None else None
        return {
            "status": "ok",
            "service": "python-kokoro",
            "python": ".".join(map(str, sys.version_info[:3])),
            "pythonExecutable": sys.executable,
            "pythonPrefix": sys.prefix,
            "kokoro": kokoro_version,
            "torch": torch_version,
            "requestedDevice": self.requested_device,
            "device": self.device,
            "cudaAvailable": torch.cuda.is_available(),
            "cudaVersion": torch.version.cuda,
            "gpuName": torch.cuda.get_device_name() if self.device == "cuda" else None,
            "model": MODEL_REPO,
            "modelLoaded": bool(pipelines),
            "modelDevices": model_devices,
            "fingerprint": (
                f"kokoro={kokoro_version};torch={torch_version};"
                f"device={self.device};model={MODEL_REPO}"
            ),
        }


class KokoroRequestHandler(BaseHTTPRequestHandler):
    # urllib's short-lived health and speech calls do not pool connections.
    # Closing explicitly avoids harmless Windows reset tracebacks while the
    # handler waits for another request on an abandoned keep-alive socket.
    protocol_version = "HTTP/1.0"
    server_version = "PythonKokoro/1.0"

    @property
    def service(self) -> KokoroService:
        return self.server.kokoro_service  # type: ignore[attr-defined]

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()

    def send_json(self, status: HTTPStatus, value: Any) -> None:
        body = json.dumps(value, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def read_json(self) -> dict[str, Any]:
        try:
            length = int(self.headers.get("Content-Length", ""))
        except ValueError as error:
            raise ValueError("Content-Length must be an integer") from error
        if length <= 0 or length > MAX_REQUEST_BYTES:
            raise ValueError("Request body is empty or too large")
        try:
            value = json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as error:
            raise ValueError("Request body must be valid UTF-8 JSON") from error
        if not isinstance(value, dict):
            raise ValueError("Request body must be a JSON object")
        return value

    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        path = urlsplit(self.path).path
        if path == "/health":
            self.send_json(HTTPStatus.OK, self.service.health())
            return
        if path == "/v1/models":
            self.send_json(
                HTTPStatus.OK,
                {"object": "list", "data": [{"id": "kokoro", "object": "model"}]},
            )
            return
        self.send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found"})

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if urlsplit(self.path).path != "/v1/audio/speech":
            self.send_json(HTTPStatus.NOT_FOUND, {"error": "Endpoint not found"})
            return
        try:
            payload = self.read_json()
            text = payload.get("input")
            voice = payload.get("voice")
            speed = payload.get("speed", 1.0)
            response_format = payload.get("response_format", "wav")
            lang_code = payload.get("language", "a")
            if not isinstance(text, str) or not text.strip():
                raise ValueError("input must be a non-empty string")
            if not isinstance(voice, str) or not voice.strip():
                raise ValueError("voice must be a non-empty string")
            if not isinstance(speed, (int, float)) or not 0.5 <= float(speed) <= 2.0:
                raise ValueError("speed must be a number between 0.5 and 2.0")
            if response_format != "wav":
                raise ValueError("response_format must be wav")
            if not isinstance(lang_code, str) or not lang_code:
                raise ValueError("language must be a non-empty Kokoro language code")
            body, metrics = self.service.synthesize(
                text.strip(), voice.strip(), float(speed), lang_code
            )
        except ValueError as error:
            self.send_json(HTTPStatus.BAD_REQUEST, {"error": str(error)})
            return
        except Exception as error:  # noqa: BLE001 - report model failures to client
            LOG.exception("Speech synthesis failed")
            self.send_json(HTTPStatus.INTERNAL_SERVER_ERROR, {"error": str(error)})
            return

        self.send_response(HTTPStatus.OK)
        self.send_header("Content-Type", "audio/wav")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for name, value in metrics.items():
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: Any) -> None:
        LOG.info("%s - %s", self.address_string(), format % args)


def create_server(host: str, port: int, device: str) -> ThreadingHTTPServer:
    # Initialize PyTorch/CUDA before binding the socket. Otherwise health probes
    # can connect while CUDA is still loading, time out, and leave queued
    # requests whose response sockets have already been abandoned.
    service = KokoroService(device)
    server = ThreadingHTTPServer((host, port), KokoroRequestHandler)
    server.kokoro_service = service  # type: ignore[attr-defined]
    return server


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Serve official Kokoro from Python 3.12")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8302)
    parser.add_argument(
        "--device",
        choices=("auto", "cpu", "cuda"),
        default=os.environ.get("KOKORO_DEVICE", os.environ.get("PODCAST_DEVICE", "auto")),
    )
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    if sys.version_info[:2] != (3, 12):
        raise RuntimeError(
            f"python-kokoro requires Python 3.12; current interpreter is {sys.version.split()[0]}"
        )
    args = parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="[tts] %(levelname)s %(message)s")
    server = create_server(args.host, args.port, args.device)
    print(
        f"[tts] Official Kokoro service: http://{args.host}:{server.server_port} "
        f"({server.kokoro_service.device})",  # type: ignore[attr-defined]
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n[tts] Stopped.", flush=True)
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
