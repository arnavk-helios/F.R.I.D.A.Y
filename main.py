from __future__ import annotations

import argparse
import subprocess
from pathlib import Path

from core.audio_engine import AudioEngine
from core.brain import HybridBrain
from core.executor import SandboxedExecutor
from core.local_parser import DeterministicLocalParser
from core.voice import JarvisVoice
from ui.hud_overlay import AssistantState, HUDController
from ui.menubar import VayuController


def _play_wake_chime() -> None:
    """Plays a crisp non-blocking macOS chime when 'Hey Jarvis' triggers."""
    sound = Path("/System/Library/Sounds/Pop.aiff")
    if sound.exists():
        subprocess.Popen(
            ["afplay", str(sound)],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )


def build_vayu_controller(enable_native_window: bool = True) -> VayuController:
    executor = SandboxedExecutor()
    parser = DeterministicLocalParser(executor.config)
    friday_model = Path("models/hey_friday.onnx")
    audio = AudioEngine(
        wakeword_model_paths=[str(friday_model)] if friday_model.exists() else None
    )

    hud = HUDController(
        on_kill_mic=audio.disable_mic,
        enable_native_window=enable_native_window,
    )
    controller = VayuController(
        executor=executor,
        parser=parser,
        audio_engine=audio,
        hud=hud,
        voice=JarvisVoice(enabled=enable_native_window),
    )
    hud.on_kill_mic = controller.engage_hard_off

    brain = HybridBrain(
        executor=executor,
        parser=parser,
        execute_tool_fn=controller._execute_with_confirmation,
        is_cloud_enabled_fn=lambda: controller.cloud_mode_enabled,
    )
    controller.fallback_router = brain.route

    orig_on_wake = audio.on_wake_confirm

    def _on_wake_with_chime(label: str, score: float) -> bool:
        _play_wake_chime()
        if orig_on_wake is not None:
            return orig_on_wake(label, score)
        return True

    audio.on_wake_confirm = _on_wake_with_chime

    orig_process = controller.process_transcript

    def _logged_process_transcript(raw_transcript: str) -> str:
        outcome = orig_process(raw_transcript)
        print(f"[Vayu Action] {outcome}")
        return outcome

    controller.process_transcript = _logged_process_transcript  # type: ignore[method-assign]
    audio.on_transcript = lambda text: _logged_process_transcript(text)

    return controller


def run_menubar(auto_listen: bool = False) -> None:
    import rumps

    class VayuApp(rumps.App):
        def __init__(self) -> None:
            super().__init__("🔴 Vayu", quit_button="Quit Vayu")
            self.status_item = rumps.MenuItem("Status: Hard Off (Stream Torn Down)")
            self.mic_item = rumps.MenuItem(
                "Enable Microphone (Active)",
                callback=lambda _: self.controller.toggle_mic(),
            )
            self.cloud_item = rumps.MenuItem(
                "Privacy Mode: 100% Offline (Click for Cloud Opt-In)",
                callback=lambda _: self.controller.toggle_cloud_mode(),
            )
            self.menu = [self.status_item, None, self.mic_item, self.cloud_item]

            self.controller = build_vayu_controller(enable_native_window=True)
            self.controller.on_state_change = self._on_state_change

            if auto_listen:
                self.controller.enable_listening()
                self._on_state_change(
                    AssistantState.ACTIVE, self.controller.cloud_mode_enabled
                )
                print(
                    "[Vayu] Microphone ACTIVE (🟢) — say 'Hey Friday' (or 'Hey Jarvis' until models/hey_friday.onnx is added) followed by your command."
                )

        def _on_state_change(self, state: AssistantState, cloud_enabled: bool) -> None:
            badges = {
                AssistantState.ACTIVE: (
                    "🟢 Vayu",
                    "Status: Active (Listening Locally)",
                    "Disable Microphone (Hard Off)",
                ),
                AssistantState.LISTENING: (
                    "🔵 Vayu",
                    "Status: Listening to Command...",
                    "Disable Microphone (Hard Off)",
                ),
                AssistantState.PROCESSING: (
                    "🔵 Vayu",
                    "Status: Executing Safely...",
                    "Disable Microphone (Hard Off)",
                ),
                AssistantState.HARD_OFF: (
                    "🔴 Vayu",
                    "Status: Hard Off (Stream Torn Down)",
                    "Enable Microphone (Active)",
                ),
            }
            self.title, self.status_item.title, self.mic_item.title = badges[state]
            self.cloud_item.title = (
                "Privacy Mode: Cloud Opt-In (Sanitized Aliases)"
                if cloud_enabled
                else "Privacy Mode: 100% Offline (Click for Cloud Opt-In)"
            )

    print(
        "[Vayu] Menu bar daemon started (look for '🔴 Vayu' in top-right bar; click Desktop if hidden behind notch)."
    )
    VayuApp().run()


if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser(description="Vayu Privacy-First Voice OS Agent")
    arg_parser.add_argument(
        "--listen",
        action="store_true",
        help="Immediately enable microphone wake-word detection on startup.",
    )
    arg_parser.add_argument(
        "--cmd",
        type=str,
        default=None,
        help="Run a single text command through the full Phase 1-4 pipeline and exit.",
    )
    args = arg_parser.parse_args()

    if args.cmd:
        ctrl = build_vayu_controller(enable_native_window=False)
        ctrl.process_transcript(args.cmd)
    else:
        run_menubar(auto_listen=args.listen)