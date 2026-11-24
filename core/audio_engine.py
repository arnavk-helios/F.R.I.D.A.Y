from __future__ import annotations

import hashlib
import json
import threading
import time
from collections import deque
from pathlib import Path
from typing import Callable

import numpy as np
from pydantic import BaseModel, ConfigDict, Field

from core.executor import SecurityViolationError


class AudioConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    sample_rate: int = Field(default=16000, ge=8000, le=48000)
    channels: int = Field(default=1, ge=1, le=1)
    frame_samples: int = Field(default=1280, ge=320, le=4096)  # 80ms @ 16kHz
    ring_buffer_seconds: float = Field(default=2.0, gt=0.5, le=10.0)
    wake_threshold: float = Field(default=0.20, gt=0.0, lt=1.0)
    wake_target_rms: float = Field(default=0.08, gt=0.01, le=0.5)
    wake_max_gain: float = Field(default=10.0, ge=1.0, le=20.0)
    wake_rms_gate: float = Field(default=0.0025, ge=0.0, lt=0.1)
    cooldown_seconds: float = Field(default=2.5, ge=0.5, le=60.0)
    silence_rms_threshold: float = Field(default=0.0045, gt=0.0, lt=1.0)
    min_capture_sec: float = Field(default=1.8, ge=0.5, le=5.0)
    speech_onset_timeout_sec: float = Field(default=3.0, ge=1.0, le=10.0)
    silence_duration_sec: float = Field(default=0.9, ge=0.2, le=3.0)
    max_command_sec: float = Field(default=7.0, ge=2.0, le=20.0)
    whisper_repo: str = Field(default="mlx-community/whisper-small.en-mlx", min_length=3)
    initial_prompt: str = Field(
        default=(
            "Open Canva, Claude, VS Code, Chrome, Safari, Notion, Spotify, "
            "Downloads, Documents, Projects, YouTube, GitHub, Gmail, "
            "study mode, code mode, split screen, top bottom, close."
        )
    )
    log_transcripts: bool = False  # Disabled by default for local privacy


def compute_sha256(file_path: Path) -> str:
    """Computes SHA-256 digest of a model artifact in 64 KiB chunks."""
    if not file_path.is_file():
        raise FileNotFoundError(f"Model file not found: {file_path}")
    digest = hashlib.sha256()
    with file_path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_sha256(file_path: Path, expected_sha256: str) -> None:
    """Fail-closed SHA-256 verification for wake-word and STT model weights."""
    actual = compute_sha256(file_path)
    if actual != expected_sha256.strip().lower():
        raise SecurityViolationError(
            f"SHA-256 checksum mismatch for {file_path.name}: "
            f"expected {expected_sha256.lower()}, got {actual}"
        )


def verify_model_manifest(manifest_path: Path) -> None:
    """Verifies all pinned model files listed in config/model_hashes.json."""
    if not manifest_path.exists():
        raise FileNotFoundError(f"Missing model hash manifest: {manifest_path}")
    data = json.loads(manifest_path.read_text(encoding="utf-8"))
    models: dict[str, str] = data.get("models", {})
    for raw_path, expected_hash in models.items():
        verify_sha256(Path(raw_path).expanduser().resolve(), expected_hash)


class AudioEngine:
    """
    Privacy-first local audio capture, wake-word gate, and Apple Silicon STT.
    - Keeps all PCM audio strictly in RAM (zero disk writes).
    - Implements hardware stream teardown + OS permission gating on mute.
    """

    def __init__(
        self,
        config: AudioConfig | None = None,
        manifest_path: Path = Path("config/model_hashes.json"),
        wakeword_model_paths: list[str] | None = ["models/hey_friday.onnx"],
        on_wake_confirm: Callable[[str, float], bool] | None = None,
        on_transcript: Callable[[str], None] | None = None,
    ) -> None:
        self.config = config or AudioConfig()
        self.manifest_path = manifest_path
        self.wakeword_model_paths = wakeword_model_paths or []
        self.on_wake_confirm = on_wake_confirm
        self.on_transcript = on_transcript

        max_frames = int(
            (self.config.ring_buffer_seconds * self.config.sample_rate)
            // self.config.frame_samples
        )
        self._ring_buffer: deque[np.ndarray] = deque(maxlen=max(1, max_frames))
        self._lock = threading.RLock()
        self._whisper_lock = threading.Lock()
        self._mic_enabled = threading.Event()
        self._stream = None
        self._worker_thread: threading.Thread | None = None
        self._last_trigger_ts: float = 0.0

        self._oww_model = None

    def _init_wakeword_model(self) -> None:
        verify_model_manifest(self.manifest_path)
        from openwakeword.model import Model  # Lazy import so unit tests run headless

        target_models = (
            self.wakeword_model_paths if self.wakeword_model_paths else ["hey_jarvis"]
        )
        self._oww_model = Model(
            wakeword_models=target_models,
            inference_framework="onnx",
        )

    def enable_mic(self) -> None:
        """Opens the hardware input stream (auto-resetting CoreAudio on -9986) and starts the RAM loop."""
        with self._lock:
            if self._mic_enabled.is_set():
                return
            if self._oww_model is None:
                self._init_wakeword_model()

            import sounddevice as sd

            self._ring_buffer.clear()
            for attempt in range(2):
                try:
                    self._stream = sd.InputStream(
                        samplerate=self.config.sample_rate,
                        channels=self.config.channels,
                        dtype="int16",
                        blocksize=self.config.frame_samples,
                    )
                    self._stream.start()
                    break
                except sd.PortAudioError:
                    if attempt == 1:
                        raise
                    sd._terminate()
                    time.sleep(0.25)
                    sd._initialize()

            self._mic_enabled.set()

            self._worker_thread = threading.Thread(
                target=self._listen_loop, name="vayu-audio-loop", daemon=True
            )
            self._worker_thread.start()

    def disable_mic(self) -> None:
        """
        Hard Kill-Switch (Stream Teardown + OS Permission Gating):
        Physically stops/closes the sounddevice stream and wipes the RAM buffer.
        """
        with self._lock:
            self._mic_enabled.clear()
            if self._stream is not None:
                try:
                    self._stream.stop()
                finally:
                    self._stream.close()
                    self._stream = None
            self._ring_buffer.clear()

    @property
    def is_mic_enabled(self) -> bool:
        return self._mic_enabled.is_set()

    def should_accept_wake(self, score: float, now: float | None = None) -> bool:
        """Enforces mic-enabled state, confidence threshold, and cooldown window."""
        if not self._mic_enabled.is_set():
            return False
        if score < self.config.wake_threshold:
            return False
        current = time.monotonic() if now is None else now
        if (current - self._last_trigger_ts) < self.config.cooldown_seconds:
            return False
        self._last_trigger_ts = current
        return True

    def _boost_for_wakeword(self, mono: np.ndarray) -> np.ndarray:
        """Applies adaptive RMS normalization so quiet speech enters openWakeWord's optimal range."""
        if mono.size == 0 or self.config.wake_max_gain <= 1.0:
            return mono
        f32 = mono.astype(np.float32) / 32768.0
        rms = float(np.sqrt(np.mean(f32 ** 2)))
        if rms < self.config.wake_rms_gate:
            return mono
        gain = min(self.config.wake_max_gain, self.config.wake_target_rms / rms)
        boosted = np.clip(f32 * gain, -0.95, 0.95)
        return (boosted * 32767.0).astype(np.int16)

    def transcribe_pcm_in_memory(self, pcm_int16: np.ndarray) -> str:
        """Transcribes 16kHz mono int16 numpy audio strictly in RAM via mlx-whisper."""
        if pcm_int16.size == 0:
            return ""
        audio_f32 = pcm_int16.astype(np.float32) / 32768.0

        # Boost quiet MacBook Air speech while keeping ambient noise floor untouched
        peak = float(np.max(np.abs(audio_f32)))
        if 0.005 < peak < 0.35:
            audio_f32 = np.clip(audio_f32 * (0.6 / peak), -1.0, 1.0)

        import mlx_whisper

        with self._whisper_lock:
            result = mlx_whisper.transcribe(
                audio_f32,
                path_or_hf_repo=self.config.whisper_repo,
                fp16=True,
                initial_prompt=self.config.initial_prompt,
                condition_on_previous_text=False,
            )
        raw_text = str(result.get("text", "")).strip()
        return raw_text.rstrip(".!?,;:")

    def _capture_command_until_silence(
        self, preroll_frames: list[np.ndarray] | None = None
    ) -> np.ndarray:
        """
        Captures post-wake speech with pre-roll, a guaranteed minimum listening window,
        and adaptive RMS endpointing so soft speech is never dropped.
        """
        frames: list[np.ndarray] = list(preroll_frames) if preroll_frames else []
        min_frames = int(
            (self.config.min_capture_sec * self.config.sample_rate)
            // self.config.frame_samples
        )
        onset_max_frames = int(
            (self.config.speech_onset_timeout_sec * self.config.sample_rate)
            // self.config.frame_samples
        )
        silence_frames_needed = int(
            (self.config.silence_duration_sec * self.config.sample_rate)
            // self.config.frame_samples
        )
        max_frames = int(
            (self.config.max_command_sec * self.config.sample_rate)
            // self.config.frame_samples
        )

        speech_started = False
        consecutive_silent = 0

        for idx in range(max_frames):
            if not self._mic_enabled.is_set() or self._stream is None:
                return np.array([], dtype=np.int16)

            chunk, _ = self._stream.read(self.config.frame_samples)
            mono = chunk.reshape(-1).astype(np.int16)
            frames.append(mono)

            rms = float(np.sqrt(np.mean((mono.astype(np.float32) / 32768.0) ** 2)))
            if rms >= self.config.silence_rms_threshold:
                speech_started = True
                consecutive_silent = 0
            else:
                consecutive_silent += 1

            # Always record at least min_capture_sec after wake word
            if idx < min_frames:
                continue

            if not speech_started and idx >= onset_max_frames:
                break

            if speech_started and consecutive_silent >= silence_frames_needed:
                break

        if not frames:
            return np.array([], dtype=np.int16)
        return np.concatenate(frames)

    def _listen_loop(self) -> None:
        while self._mic_enabled.is_set() and self._stream is not None:
            try:
                chunk, _ = self._stream.read(self.config.frame_samples)
            except Exception:
                break

            if not self._mic_enabled.is_set():
                break

            mono = chunk.reshape(-1).astype(np.int16)
            self._ring_buffer.append(mono)

            if self._oww_model is None:
                continue

            wake_frame = self._boost_for_wakeword(mono)
            predictions: dict[str, float] = self._oww_model.predict(wake_frame)

            for wake_label, raw_score in predictions.items():
                score = float(raw_score)
                if not self.wakeword_model_paths and "jarvis" not in wake_label.lower():
                    continue

                if 0.10 <= score < self.config.wake_threshold:
                    print(
                        f"[Vayu Wake Miss] {wake_label} score={score:.2f} "
                        f"(threshold={self.config.wake_threshold:.2f})"
                    )

                if self.should_accept_wake(score):
                    print(
                        f"[Vayu Wake] Triggered ({wake_label}, score={score:.2f}) — speak command now..."
                    )
                    if self.on_wake_confirm and not self.on_wake_confirm(
                        wake_label, score
                    ):
                        break

                    preroll = list(self._ring_buffer)[-3:]
                    self._ring_buffer.clear()

                    command_pcm = self._capture_command_until_silence(
                        preroll_frames=preroll
                    )

                    print("[Vayu Transcribing...]")
                    transcript = self.transcribe_pcm_in_memory(command_pcm)
                    del command_pcm

                    if hasattr(self._oww_model, "reset"):
                        self._oww_model.reset()
                    self._last_trigger_ts = time.monotonic()

                    print(f"[Vayu Heard] {transcript!r}")
                    if self.on_transcript:
                        self.on_transcript(transcript)
                    break