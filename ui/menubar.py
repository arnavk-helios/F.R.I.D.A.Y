from __future__ import annotations

import re
from typing import Callable

from core.audio_engine import AudioEngine
from core.executor import (
    ConfirmationRequiredError,
    SandboxedExecutor,
    SecurityViolationError,
)
from core.local_parser import DeterministicLocalParser
from ui.hud_overlay import AssistantState, HUDController

CLOUD_FREE_TIER_DISCLOSURE = (
    "Cloud Mode is opt-in and disabled by default.\n\n"
    "When enabled, only PII-scrubbed alias tokens (e.g., 'open [study_notes]') are sent to Gemini. "
    "Note: Requests sent using a Gemini API free-tier key may be logged by Google for product improvement. "
    "Use a paid API billing tier if you require zero-training data guarantees.\n\n"
    "Enable Opt-In Cloud Mode?"
)


def _run_ui_on_main_thread(fn: Callable[[], None]) -> None:
    """Safely dispatches rumps status bar updates onto the macOS main runloop."""
    try:
        from AppKit import NSThread
        from PyObjCTools import AppHelper

        if NSThread.isMainThread():
            fn()
        else:
            AppHelper.callAfter(fn)
    except Exception:
        fn()


class VayuController:
    """
    Core UI & orchestration controller bridging Menu Bar, Floating HUD,
    Hardware Stream Teardown Kill-Switch, and Sandboxed Executor.
    """

    WAKE_PREFIX_RE = re.compile(
        r"^(?:hey\s+(?:vayu|jarvis)|vayu|jarvis)[,\s]*", flags=re.IGNORECASE
    )

    def __init__(
        self,
        executor: SandboxedExecutor,
        parser: DeterministicLocalParser,
        audio_engine: AudioEngine,
        hud: HUDController | None = None,
        fallback_router: Callable[[str], str] | None = None,
        on_state_change: Callable[[AssistantState, bool], None] | None = None,
    ) -> None:
        self.executor = executor
        self.parser = parser
        self.audio_engine = audio_engine
        self.fallback_router = fallback_router
        self.on_state_change = on_state_change

        self.cloud_mode_enabled: bool = False  # Strictly opt-in; defaults to 100% offline
        self.state: AssistantState = AssistantState.HARD_OFF

        self.hud = hud or HUDController(on_kill_mic=self.engage_hard_off)
        self.audio_engine.on_wake_confirm = self._handle_wake_detected
        self.audio_engine.on_transcript = self.process_transcript

    def _set_state(self, new_state: AssistantState) -> None:
        self.state = new_state
        if self.on_state_change:
            self.on_state_change(new_state, self.cloud_mode_enabled)

    def enable_listening(self) -> None:
        """Opens the hardware microphone stream and enters Active wake-word mode."""
        self.audio_engine.enable_mic()
        self._set_state(AssistantState.ACTIVE)
        self.hud.show_status(
            state=AssistantState.ACTIVE,
            headline="🟢 Vayu: Active",
            detail='Listening locally for wake word',
            auto_hide_seconds=2.0,
        )

    def engage_hard_off(self) -> None:
        """Hard Kill-Switch: tears down sounddevice stream and wipes RAM buffer."""
        self.audio_engine.disable_mic()
        self._set_state(AssistantState.HARD_OFF)

    def toggle_mic(self) -> None:
        if self.audio_engine.is_mic_enabled:
            self.engage_hard_off()
            self.hud.show_status(
                state=AssistantState.HARD_OFF,
                headline="🔴 Vayu: Hard Off",
                detail="Stream torn down + OS permission gated",
                auto_hide_seconds=2.5,
            )
        else:
            self.enable_listening()

    def toggle_cloud_mode(self) -> bool:
        """Toggles opt-in cloud mode after explicit free-tier data-use confirmation."""
        if self.cloud_mode_enabled:
            self.cloud_mode_enabled = False
            self._set_state(self.state)
            return False

        approved = self.hud.request_confirmation(
            title="Opt-In Cloud Mode Disclosure",
            prompt=CLOUD_FREE_TIER_DISCLOSURE,
        )
        self.cloud_mode_enabled = approved
        self._set_state(self.state)
        return self.cloud_mode_enabled

    def _handle_wake_detected(self, wake_label: str, score: float) -> bool:
        """Secondary wake-word gate: ignores triggers if muted and pops up HUD with KILL MIC."""
        if not self.audio_engine.is_mic_enabled:
            return False
        self._set_state(AssistantState.LISTENING)
        self.hud.show_status(
            state=AssistantState.LISTENING,
            headline="🔵 Vayu: Listening...",
            detail=f"Wake detected ({wake_label}) — speak command or click KILL MIC",
            auto_hide_seconds=None,
        )
        return True

    def process_transcript(self, raw_transcript: str) -> str:
        """Routes transcribed text through Tier 1 parser, confirmation gate, and sandboxed executor."""
        cleaned_transcript = self.WAKE_PREFIX_RE.sub(
            "", (raw_transcript or "").strip()
        ).strip()
        if not cleaned_transcript:
            self._finish_command("Cancelled: No speech detected")
            return "Cancelled: No speech detected"

        self._set_state(AssistantState.PROCESSING)
        self.hud.show_status(
            state=AssistantState.PROCESSING,
            headline="⚙️ Processing Command",
            detail=f'Heard: "{cleaned_transcript}"',
            auto_hide_seconds=None,
        )

        try:
            action = self.parser.parse(cleaned_transcript)
            if action is None:
                if self.fallback_router is not None:
                    outcome = self.fallback_router(cleaned_transcript)
                else:
                    outcome = "Unrecognized command (Phase 4 LLM router not attached yet)"
                self._finish_command(outcome)
                return outcome

            outcome = self._execute_with_confirmation(action["tool"], action["args"])
            self._finish_command(outcome)
            return outcome

        except SecurityViolationError as exc:
            msg = f"Blocked: {exc}"
            self._finish_command(msg)
            return msg
        except Exception as exc:
            msg = f"Error: {exc}"
            self._finish_command(msg)
            return msg

    def _execute_with_confirmation(self, tool_name: str, args: dict) -> str:
        try:
            return self.executor.dispatch(tool_name, args)
        except ConfirmationRequiredError as confirm_err:
            approved = self.hud.request_confirmation(
                title="Confirm Action",
                prompt=confirm_err.prompt,
            )
            if not approved:
                return f"Cancelled: {confirm_err.prompt}"

            payload = confirm_err.action_payload
            return self.executor.dispatch(payload["tool"], payload["args"])

    def _finish_command(self, summary: str) -> None:
        next_state = (
            AssistantState.ACTIVE
            if self.audio_engine.is_mic_enabled
            else AssistantState.HARD_OFF
        )
        self._set_state(next_state)
        is_blocked_or_error = summary.startswith(("Blocked:", "Cancelled:", "Error:"))
        self.hud.show_status(
            state=next_state,
            headline="🛡️ Vayu" if is_blocked_or_error else "✅ Vayu",
            detail=summary,
            auto_hide_seconds=2.5,
        )


def run_menubar_app() -> None:
    """Starts the native macOS rumps status bar application."""
    import rumps

    executor = SandboxedExecutor()
    parser = DeterministicLocalParser(executor.config)
    audio = AudioEngine()

    class VayuStatusBarApp(rumps.App):
        def __init__(self) -> None:
            super().__init__("🔴 Vayu", quit_button="Quit Vayu")
            self.status_item = rumps.MenuItem("Status: Hard Off (Stream Torn Down)")
            self.mic_toggle_item = rumps.MenuItem(
                "Enable Microphone (Active)", callback=self._on_toggle_mic
            )
            self.cloud_toggle_item = rumps.MenuItem(
                "Privacy Mode: 100% Offline (Click for Cloud Opt-In)",
                callback=self._on_toggle_cloud,
            )
            self.study_mode_item = rumps.MenuItem(
                "Run Study Mode (Split)",
                callback=lambda _: self.controller.process_transcript("study mode"),
            )
            self.code_mode_item = rumps.MenuItem(
                "Run Code Mode (Split)",
                callback=lambda _: self.controller.process_transcript("code mode"),
            )

            self.menu = [
                self.status_item,
                None,
                self.mic_toggle_item,
                self.cloud_toggle_item,
                None,
                self.study_mode_item,
                self.code_mode_item,
            ]

            self.controller = VayuController(
                executor=executor,
                parser=parser,
                audio_engine=audio,
                on_state_change=self._sync_menu_ui,
            )

        def _sync_menu_ui(self, state: AssistantState, cloud_enabled: bool) -> None:
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
            title, status_text, btn_label = badges[state]

            def _apply() -> None:
                self.title = title
                self.status_item.title = status_text
                self.mic_toggle_item.title = btn_label
                self.cloud_toggle_item.title = (
                    "Privacy Mode: Cloud Opt-In (Sanitized Aliases)"
                    if cloud_enabled
                    else "Privacy Mode: 100% Offline (Click for Cloud Opt-In)"
                )

            _run_ui_on_main_thread(_apply)

        def _on_toggle_mic(self, _sender) -> None:
            self.controller.toggle_mic()

        def _on_toggle_cloud(self, _sender) -> None:
            self.controller.toggle_cloud_mode()

    VayuStatusBarApp().run()


if __name__ == "__main__":
    run_menubar_app()