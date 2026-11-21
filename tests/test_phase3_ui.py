import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

from core.audio_engine import AudioConfig, AudioEngine
from core.executor import SandboxedExecutor
from core.local_parser import DeterministicLocalParser
from ui.hud_overlay import AssistantState, HUDController
from ui.menubar import VayuController


class TestPhase3MenuBarAndHUD(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp_dir.name)

        self.protected_path = self.base / "protected_bundles.json"
        self.protected_path.write_text(
            json.dumps({"protected_bundle_ids": ["com.apple.finder", "com.apple.Terminal"]}),
            encoding="utf-8",
        )

        self.aliases_path = self.base / "aliases.json"
        self.aliases_path.write_text(
            json.dumps(
                {
                    "default_browser": {
                        "bundle_id": "com.google.Chrome",
                        "path": "/Applications/Google Chrome.app",
                    },
                    "apps": {
                        "spotify": {
                            "spoken_names": ["spotify"],
                            "bundle_id": "com.spotify.client",
                            "path": "/Applications/Spotify.app",
                        }
                    },
                    "folders": {},
                    "bookmarks": {
                        "youtube": {"spoken_names": ["youtube"], "url": "https://www.youtube.com"}
                    },
                    "modes": {},
                }
            ),
            encoding="utf-8",
        )
        os.chmod(self.aliases_path, 0o600)

        self.manifest = self.base / "model_hashes.json"
        self.manifest.write_text(json.dumps({"models": {}}), encoding="utf-8")

        self.executor = SandboxedExecutor(
            aliases_path=self.aliases_path,
            protected_path=self.protected_path,
            enforce_permissions=True,
        )
        self.parser = DeterministicLocalParser(self.executor.config)
        self.audio = AudioEngine(config=AudioConfig(), manifest_path=self.manifest)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_hud_kill_mic_button_tears_down_stream(self) -> None:
        mock_stream = MagicMock()
        self.audio._stream = mock_stream
        self.audio._mic_enabled.set()

        hud = HUDController(
            on_kill_mic=self.audio.disable_mic,
            confirm_handler=lambda _t, _p: False,
            enable_native_window=False,
        )
        controller = VayuController(
            executor=self.executor,
            parser=self.parser,
            audio_engine=self.audio,
            hud=hud,
        )
        controller.state = AssistantState.LISTENING

        # Simulate clicking red KILL MIC on the floating HUD
        hud.trigger_kill_switch()

        self.assertFalse(self.audio.is_mic_enabled)
        mock_stream.stop.assert_called_once()
        mock_stream.close.assert_called_once()
        self.assertEqual(hud.view.state, AssistantState.HARD_OFF)

    def test_quit_action_blocked_when_denied_and_executed_when_approved(self) -> None:
        decisions = iter([False, True])
        hud = HUDController(
            on_kill_mic=self.audio.disable_mic,
            confirm_handler=lambda _t, _p: next(decisions),
            enable_native_window=False,
        )
        controller = VayuController(
            executor=self.executor,
            parser=self.parser,
            audio_engine=self.audio,
            hud=hud,
        )

        with patch("subprocess.run") as mock_run:
            # First attempt: User clicks Deny -> osascript must NOT be called
            res_denied = controller.process_transcript("close spotify")
            self.assertIn("Cancelled:", res_denied)
            mock_run.assert_not_called()

            # Second attempt: User clicks Allow -> osascript called with argv
            res_allowed = controller.process_transcript("close spotify")
            self.assertEqual(res_allowed, "Closed app: spotify")
            mock_run.assert_called_once()
            cmd_args = mock_run.call_args[0][0]
            self.assertEqual(cmd_args[0], "osascript")
            self.assertEqual(cmd_args[-1], "com.spotify.client")

    def test_unbookmarked_domain_requires_hud_confirmation(self) -> None:
        hud = HUDController(
            on_kill_mic=self.audio.disable_mic,
            confirm_handler=lambda _t, _p: False,
            enable_native_window=False,
        )
        controller = VayuController(
            executor=self.executor,
            parser=self.parser,
            audio_engine=self.audio,
            hud=hud,
        )

        with patch("subprocess.run") as mock_run:
            result = controller.process_transcript("open example.com")
            self.assertIn("Cancelled:", result)
            mock_run.assert_not_called()

    def test_cloud_mode_defaults_offline_and_requires_opt_in_disclosure(self) -> None:
        decisions = iter([False, True])
        hud = HUDController(
            on_kill_mic=self.audio.disable_mic,
            confirm_handler=lambda _t, _p: next(decisions),
            enable_native_window=False,
        )
        controller = VayuController(
            executor=self.executor,
            parser=self.parser,
            audio_engine=self.audio,
            hud=hud,
        )

        self.assertFalse(controller.cloud_mode_enabled)
        # Declining disclosure keeps Cloud Mode off
        self.assertFalse(controller.toggle_cloud_mode())
        self.assertFalse(controller.cloud_mode_enabled)
        # Accepting disclosure enables Cloud Mode
        self.assertTrue(controller.toggle_cloud_mode())
        self.assertTrue(controller.cloud_mode_enabled)


if __name__ == "__main__":
    unittest.main()