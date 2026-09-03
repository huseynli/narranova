from __future__ import annotations

import io
import json
import tempfile
import unittest
import urllib.request
import wave
from pathlib import Path
from unittest.mock import patch

from narranova.audio import validate_wave
from narranova.providers import (
    OpenMossConfig,
    OpenMossProvider,
    SynthesisRequest,
    openmoss_sampling_from_form,
)


def reference_wave(path: Path) -> None:
    with wave.open(str(path), "wb") as output:
        output.setnchannels(1)
        output.setsampwidth(2)
        output.setframerate(16_000)
        output.writeframes(b"\x00\x00" * 160)


class FakeResponse:
    def __init__(self, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.body = io.BytesIO(body)
        self.headers = headers or {}

    def read(self, size: int = -1) -> bytes:
        return self.body.read(size)

    def __enter__(self) -> "FakeResponse":
        return self

    def __exit__(self, *args: object) -> None:
        return None


class OpenMossProviderTests(unittest.TestCase):
    def test_instruction_only_request_omits_reference_audio(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "audition.wav"
            response = FakeResponse(
                b"\x01\x00" * 240,
                {"X-MOSS-Sample-Rate": "24000", "X-MOSS-Channels": "1"},
            )
            provider = OpenMossProvider(OpenMossConfig("http://moss.local:8000/tts"))
            request = SynthesisRequest(
                text="Try this voice.",
                destination=destination,
                instruction="A warm, measured narrator.",
            )

            with patch.object(urllib.request, "urlopen", return_value=response) as urlopen:
                provider.synthesize(request)

            sent = json.loads(urlopen.call_args.args[0].data)
            self.assertNotIn("reference_wav_b64", sent)
            self.assertNotIn("sampling", sent)
            self.assertTrue(destination.is_file())

    def test_serializes_only_explicit_sampling_overrides(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            destination = Path(temporary) / "audition.wav"
            response = FakeResponse(
                b"\x01\x00" * 240,
                {"X-MOSS-Sample-Rate": "24000", "X-MOSS-Channels": "1"},
            )
            provider = OpenMossProvider(OpenMossConfig("http://moss.local:8000/tts"))
            request = SynthesisRequest(
                text="Try this voice.",
                destination=destination,
                instruction="A warm, measured narrator.",
                seed=91,
                parameters={"audio_temperature": 0.72, "text_top_k": 40, "seed": 44},
            )

            with patch.object(urllib.request, "urlopen", return_value=response) as urlopen:
                provider.synthesize(request)

            sent = json.loads(urlopen.call_args.args[0].data)
            self.assertEqual(
                sent["sampling"],
                {"audio_temperature": 0.72, "text_top_k": 40, "seed": 91},
            )
            self.assertNotIn("audio_top_p", sent["sampling"])

    def test_voice_sampling_form_omits_blanks_and_validates_values(self) -> None:
        sampling = openmoss_sampling_from_form(
            {
                "text_temperature": "",
                "audio_top_p": "0.92",
                "audio_top_k": "64",
                "seed": "1234",
            }
        )

        self.assertEqual(
            sampling,
            {"audio_top_p": 0.92, "audio_top_k": 64, "seed": 1234},
        )
        with self.assertRaisesRegex(ValueError, "Audio top-p"):
            openmoss_sampling_from_form({"audio_top_p": "1.5"})

    def test_connection_configuration_uses_only_performance_settings(self) -> None:
        config = OpenMossConfig.from_connection(
            "http://moss.local:8000/tts",
            {
                "stream_chunk_frames": 128,
                "recommended_stream_chunk_frames": 128,
                "sampling": {"audio_temperature": 0.1},
            },
        )

        self.assertEqual(config.stream_chunk_frames, 128)
        self.assertEqual(config.max_new_tokens, 6000)

    def test_streams_safe_clone_payload_and_atomically_promotes_wave(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.wav"
            destination = root / "chunk.wav"
            reference_wave(reference)
            pcm = b"\x01\x00\x02\x00" * 240
            response = FakeResponse(
                pcm,
                {"X-MOSS-Sample-Rate": "24000", "X-MOSS-Channels": "2"},
            )
            provider = OpenMossProvider(OpenMossConfig("http://moss.local:8000/tts"))
            synthesis = SynthesisRequest(
                text="Narrate this.",
                destination=destination,
                language="English",
                instruction="A calm narrator.",
                reference_audio=reference,
                seed=42,
            )

            with patch.object(urllib.request, "urlopen", return_value=response) as urlopen:
                result = provider.synthesize(synthesis)

            sent = json.loads(urlopen.call_args.args[0].data)
            info = validate_wave(destination)
            self.assertTrue(sent["stream"])
            self.assertEqual(sent["response_format"], "pcm")
            self.assertEqual(sent["stream_chunk_frames"], 16)
            self.assertEqual(sent["max_new_tokens"], 6000)
            self.assertEqual(sent["sampling"]["seed"], 42)
            self.assertNotIn("ref_text", sent)
            self.assertEqual(info.sample_rate, 24_000)
            self.assertEqual(info.channels, 2)
            self.assertEqual(result.audio_path, destination.resolve())
            self.assertFalse((root / ".chunk.wav.part").exists())

    def test_empty_stream_never_becomes_completed_artifact(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.wav"
            destination = root / "chunk.wav"
            reference_wave(reference)
            provider = OpenMossProvider(OpenMossConfig("http://moss.local:8000/tts"))
            request = SynthesisRequest(
                text="Narrate this.",
                destination=destination,
                instruction="A calm narrator.",
                reference_audio=reference,
            )

            with patch.object(urllib.request, "urlopen", return_value=FakeResponse(b"")):
                with self.assertRaisesRegex(RuntimeError, "without audio"):
                    provider.synthesize(request)

            self.assertFalse(destination.exists())
            self.assertFalse((root / ".chunk.wav.part").exists())

    def test_rejects_attempt_to_override_safe_transport_fields(self) -> None:
        with tempfile.TemporaryDirectory() as temporary:
            root = Path(temporary)
            reference = root / "reference.wav"
            reference_wave(reference)
            provider = OpenMossProvider(OpenMossConfig("http://moss.local:8000/tts"))
            request = SynthesisRequest(
                text="Narrate this.",
                destination=root / "chunk.wav",
                instruction="A calm narrator.",
                reference_audio=reference,
                parameters={"ref_text": "unsafe"},
            )

            with self.assertRaisesRegex(ValueError, "Reserved OpenMOSS"):
                provider.synthesize(request)


if __name__ == "__main__":
    unittest.main()
