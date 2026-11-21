import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np

from core.audio_engine import (
    AudioConfig,
    AudioEngine,
    compute_sha256,
    verify_model_manifest,
)
from core.executor import SecurityViolationError


class TestPhase2AudioSecurity(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp_dir.name)
        self.fake_model = self.base / "wakeword.onnx"
        self.fake_model.write_bytes(b"valid-onnx-model-weights-v1")

        self.manifest = self.base / "model_hashes.json"
        self.manifest.write_text(
            json.dumps({"models": {str(self.fake_model): compute_sha256(self.fake_model)}}),
            encoding="utf-8",
        )

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_model_sha256_passes_and_fails_closed_on_tampering(self) -> None:
        # Valid checksum passes
        verify_model_manifest(self.manifest)

        # Tampered model bytes must fail closed
        self.fake_model.write_bytes(b"tampered-malicious-weights")
        with self.assertRaises(SecurityViolationError):
            verify_model_manifest(self.manifest)

    def test_hard_kill_switch_tears_down_stream_and_wipes_ram_buffer(self) -> None:
        engine = AudioEngine(config=AudioConfig(), manifest_path=self.manifest)
        mock_stream = MagicMock()
        engine._stream = mock_stream
        engine._mic_enabled.set()
        engine._ring_buffer.append(np.ones(1280, dtype=np.int16))

        engine.disable_mic()

        self.assertFalse(engine.is_mic_enabled)
        mock_stream.stop.assert_called_once()
        mock_stream.close.assert_called_once()
        self.assertIsNone(engine._stream)
        self.assertEqual(len(engine._ring_buffer), 0)

    def test_wake_word_cooldown_blocks_rapid_retriggers(self) -> None:
        engine = AudioEngine(
            config=AudioConfig(wake_threshold=0.6, cooldown_seconds=5.0),
            manifest_path=self.manifest,
        )
        engine._mic_enabled.set()

        # First valid trigger at t=100.0 succeeds
        self.assertTrue(engine.should_accept_wake(0.85, now=100.0))
        # Replay/TV trigger 2 seconds later is blocked by cooldown
        self.assertFalse(engine.should_accept_wake(0.95, now=102.0))
        # Trigger after 5s cooldown expires succeeds
        self.assertTrue(engine.should_accept_wake(0.85, now=105.1))

    def test_in_memory_transcription_creates_zero_files_on_disk(self) -> None:
        engine = AudioEngine(config=AudioConfig(), manifest_path=self.manifest)
        fake_pcm = np.zeros(16000, dtype=np.int16)

        mock_mlx = MagicMock()
        mock_mlx.transcribe.return_value = {"text": "open downloads"}

        with patch.dict("sys.modules", {"mlx_whisper": mock_mlx}):
            text = engine.transcribe_pcm_in_memory(fake_pcm)

        self.assertEqual(text, "open downloads")
        # Confirm numpy float32 array was passed directly in RAM, never a file path
        passed_audio = mock_mlx.transcribe.call_args[0][0]
        self.assertIsInstance(passed_audio, np.ndarray)
        self.assertEqual(passed_audio.dtype, np.float32)
        self.assertEqual(list(self.base.glob("*.wav")), [])


if __name__ == "__main__":
    unittest.main()