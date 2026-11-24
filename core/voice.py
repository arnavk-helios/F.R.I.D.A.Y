from __future__ import annotations

import re
import shutil
import subprocess
import threading

PREFERRED_FRIDAY_VOICES = (
    "Moira (Enhanced)",
    "Moira",
    "Serena (Enhanced)",
    "Serena",
    "Kate (Enhanced)",
    "Kate",
    "Stephanie",
    "Ava (Premium)",
    "Samantha",
)


def _resolve_friday_voice() -> str | None:
    """Finds the best installed female F.R.I.D.A.Y.-style voice on macOS."""
    if shutil.which("say") is None:
        return None
    try:
        out = subprocess.check_output(["say", "-v", "?"], text=True, timeout=2)
        installed: list[str] = []
        for line in out.splitlines():
            voice_label = line.split("  ")[0].strip()
            if voice_label:
                installed.append(voice_label)

        for preferred in PREFERRED_FRIDAY_VOICES:
            if preferred in installed:
                return preferred
    except Exception:
        pass
    return None


class FridayVoice:
    """Non-blocking macOS female speech synthesizer with overlap cancellation."""

    def __init__(self, enabled: bool = True, rate: int = 195) -> None:
        self.enabled = enabled
        self.rate = rate
        self._voice_name = _resolve_friday_voice() if enabled else None
        self._lock = threading.Lock()
        self._proc: subprocess.Popen | None = None

    @property
    def is_speaking(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def stop(self) -> None:
        """Stops any currently playing speech immediately."""
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
                self._proc = None

    def speak(self, text: str) -> None:
        """Speaks text asynchronously without blocking the UI or audio loop."""
        if not self.enabled or not text.strip() or shutil.which("say") is None:
            return

        cleaned = text.strip()
        cmd = ["say"]
        if self._voice_name:
            cmd.extend(["-v", self._voice_name])
        cmd.extend(["-r", str(self.rate), cleaned])

        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                self._proc.terminate()
            try:
                self._proc = subprocess.Popen(
                    cmd,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                )
            except Exception:
                self._proc = None

    @staticmethod
    def format_outcome(summary: str) -> str:
        """Converts technical executor strings into concise F.R.I.D.A.Y.-style speech."""
        msg = summary.strip()
        if not msg or msg == "Cancelled: No speech detected":
            return ""
        if msg.startswith("Unrecognized command"):
            return "I didn't catch a valid command for that."
        if msg.startswith("Blocked:"):
            return "Action blocked by security policy."
        if msg.startswith("Error:"):
            return "Unable to complete that request."
        if msg.startswith("Cancelled:"):
            return "Cancelled."

        msg = re.sub(r"\s*\((?:50_50|70_30|30_70|top_bottom)\)$", "", msg)
        msg = re.sub(r"^Opened (?:installed )?(?:app|folder|website):\s*", "Opening ", msg)
        msg = re.sub(r"^Closed app:\s*", "Closed ", msg)
        msg = msg.replace("%", " percent")
        return msg if msg.endswith((".", "!", "?")) else f"{msg}."


# Backward-compatible alias for existing imports and unit tests
JarvisVoice = FridayVoice
