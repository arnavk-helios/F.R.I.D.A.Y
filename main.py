from __future__ import annotations

import argparse
import subprocess

from core.audio_engine import AudioEngine
from core.brain import HybridBrain
from core.executor import SandboxedExecutor
from core.local_parser import DeterministicLocalParser
from ui.hud_overlay import AssistantState, HUDController
from ui.menubar import VayuController, _run_ui_on_main_thread


def _play_wake_chime() -> None:
    """Plays a non-blocking macOS system cue when wake word triggers."""
    try:
        subprocess.Popen(
            ["afplay", "/System/Library/Sounds/Pop.aiff"],
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
    except Exception:
        pass


def build_vayu_controller(enable_native_window: bool = True) -> VayuController:
    executor = SandboxedExecutor()
    parser = DeterministicLocalParser(executor.config)
    audio = AudioEngine()

    hud = HUDController(
        on_kill_mic=audio.disable_mic,
        enable_native_window=enable_native_window,
    )
    controller = VayuController(
        executor=executor,
        parser=parser,
        audio_engine=audio,
        hud=hud,
    )

    orig_wake_confirm = controller._handle_wake_detected

    def _wake_with_chime(wake_label: str, score: float) -> bool:
        accepted = orig_wake_confirm(wake_label, score)
        if accepted:
            _play_wake_chime()
        return accepted

    audio.on_wake_confirm = _wake_with_chime

    brain = HybridBrain(
        executor=executor,
        parser=parser,
        execute_tool_fn=controller._execute_with_confirmation,
        is_cloud_enabled_fn=lambda: controller.cloud_mode_enabled,
    )
    controller.fallback_router = brain.route
    return controller


def run_menubar(auto_listen: bool = False) -> None:
    import rumps

    class VayuApp(rumps.App):
        def __init__(self) -> None:
            super().__init__("🔴 Vayu", quit_button="Quit Vayu")
            self.status_item = rumps.MenuItem("Status: Hard Off (Stream Torn Down)")
            self.mic_item = rumps.MenuItem(
                "Enable Microphone (Active)", callback=lambda _: self.controller.toggle_mic()
            )
            self.cloud_item = rumps.MenuItem(
                "Privacy Mode: 100% Offline (Click for Cloud Opt-In)",
                callback=lambda _: self.controller.toggle_cloud_mode(),
            )
            self.menu = [self.status_item, None, self.mic_item, self.cloud_item]

            self.controller = build_vayu_controller(enable_native_window=True)
            self.controller.on_state_change = self._on_state_change

            print("[Vayu] Menu bar daemon started (look for '🔴 Vayu' in top-right bar; click Desktop if hidden behind notch).")
            if auto_listen:
                _run_ui_on_main_thread(self._start_listening)

        def _start_listening(self) -> None:
            self.controller.enable_listening()
            print("[Vayu] Microphone ACTIVE (🟢) — say 'Hey Jarvis' followed by your command.")

        def _on_state_change(self, state: AssistantState, cloud_enabled: bool) -> None:
            badges = {
                AssistantState.ACTIVE: ("🟢 Vayu", "Status: Active (Listening Locally)", "Disable Microphone (Hard Off)"),
                AssistantState.LISTENING: ("🔵 Vayu", "Status: Listening to Command...", "Disable Microphone (Hard Off)"),
                AssistantState.PROCESSING: ("🔵 Vayu", "Status: Executing Safely...", "Disable Microphone (Hard Off)"),
                AssistantState.HARD_OFF: ("🔴 Vayu", "Status: Hard Off (Stream Torn Down)", "Enable Microphone (Active)"),
            }
            title, status_text, btn_label = badges[state]

            def _apply() -> None:
                self.title = title
                self.status_item.title = status_text
                self.mic_item.title = btn_label
                self.cloud_item.title = (
                    "Privacy Mode: Cloud Opt-In (Sanitized Aliases)"
                    if cloud_enabled
                    else "Privacy Mode: 100% Offline (Click for Cloud Opt-In)"
                )

            _run_ui_on_main_thread(_apply)

    VayuApp().run()


if __name__ == "__main__":
    arg_parser = argparse.ArgumentParser(description="Vayu Privacy-First Voice OS Agent")
    arg_parser.add_argument(
        "--cmd",
        type=str,
        default=None,
        help="Run a single text command through the full Phase 1-4 pipeline and exit.",
    )
    arg_parser.add_argument(
        "--listen",
        action="store_true",
        help="Enable the microphone immediately on launch instead of starting in Hard Off.",
    )
    args = arg_parser.parse_args()

    if args.cmd:
        ctrl = build_vayu_controller(enable_native_window=False)
        print(ctrl.process_transcript(args.cmd))
    else:
        run_menubar(auto_listen=args.listen)