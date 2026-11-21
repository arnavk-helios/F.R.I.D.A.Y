import json
import os
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

from core.audio_engine import AudioConfig, AudioEngine
from core.brain import HybridBrain
from core.executor import SandboxedExecutor
from core.local_parser import DeterministicLocalParser
from core.sanitizer import PIIDetectedError, PrivacySanitizer
from ui.hud_overlay import HUDController
from ui.menubar import VayuController


class TestPhase4SanitizerAndBrain(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp_dir.name)

        self.protected_path = self.base / "protected.json"
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
                        "vscode": {
                            "spoken_names": ["vs code", "code"],
                            "bundle_id": "com.microsoft.VSCode",
                            "path": "/Applications/Visual Studio Code.app",
                        },
                        "canva": {
                            "spoken_names": ["canva"],
                            "bundle_id": "com.canva.CanvaDesktop",
                            "path": "/Applications/Canva.app",
                        },
                        "spotify": {
                            "spoken_names": ["spotify", "music"],
                            "bundle_id": "com.spotify.client",
                            "path": "/Applications/Spotify.app",
                        },
                    },
                    "folders": {
                        "downloads": {"spoken_names": ["downloads"], "path": "~/Downloads"}
                    },
                    "bookmarks": {
                        "youtube": {"spoken_names": ["youtube"], "url": "https://www.youtube.com"}
                    },
                    "modes": {
                        "study_mode": {
                            "spoken_names": ["study mode"],
                            "layout": "50_50",
                            "left": "youtube",
                            "right": "canva",
                        }
                    },
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
        self.hud = HUDController(
            on_kill_mic=self.audio.disable_mic,
            confirm_handler=lambda _t, _p: True,
            enable_native_window=False,
        )
        self.controller = VayuController(
            executor=self.executor,
            parser=self.parser,
            audio_engine=self.audio,
            hud=self.hud,
        )
        self.brain = HybridBrain(
            executor=self.executor,
            parser=self.parser,
            execute_tool_fn=self.controller._execute_with_confirmation,
            is_cloud_enabled_fn=lambda: self.controller.cloud_mode_enabled,
        )
        self.controller.fallback_router = self.brain.route

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_sanitizer_replaces_aliases_and_preserves_layout_tokens(self) -> None:
        sanitizer = PrivacySanitizer(self.executor.config)
        res = sanitizer.sanitize("Hey Vayu, put vs code and youtube side by side 70 30")
        self.assertEqual(res.sanitized_text, "put [vscode] and [youtube] side by side [layout:70_30]")
        self.assertEqual(res.referenced_aliases, ["vscode", "youtube"])

    def test_pii_gate_blocks_paths_emails_numbers_and_names_from_cloud(self) -> None:
        self.controller.cloud_mode_enabled = True
        sensitive_inputs = [
            "open /Users/arnav/Desktop/Taxes_2026.pdf",
            "send file to arnav@example.com",
            "dial 9876543210 on spotify",
            "open project notes for Rahul",
        ]

        mock_genai = MagicMock()
        with patch.dict("sys.modules", {"google.genai": mock_genai, "google": MagicMock(genai=mock_genai)}):
            for text in sensitive_inputs:
                outcome = self.controller.process_transcript(text)
                self.assertIn("Blocked before LLM", outcome)
                # Ensure sensitive values are never echoed back in error output
                self.assertNotIn("arnav", outcome.lower())
                self.assertNotIn("9876543210", outcome)
                self.assertNotIn("rahul", outcome.lower())

        mock_genai.Client.assert_not_called()

    def test_offline_default_uses_local_mlx_and_runs_sandboxed_tools(self) -> None:
        self.assertFalse(self.controller.cloud_mode_enabled)
        fake_llm_json = json.dumps(
            {
                "tool_calls": [
                    {"tool": "close_app", "args": {"app_alias": "spotify", "confirmed": False}},
                    {"tool": "open_target", "args": {"target_alias": "downloads"}},
                ]
            }
        )
        mock_mlx_lm = MagicMock()
        mock_mlx_lm.load.return_value = ("fake_model", "fake_tokenizer")
        mock_mlx_lm.generate.return_value = fake_llm_json

        with patch.dict("sys.modules", {"mlx_lm": mock_mlx_lm}), \
             patch("subprocess.run") as mock_run, \
             patch("pathlib.Path.is_dir", return_value=True):
            result = self.controller.process_transcript(
                "shut down my music player and bring up my downloads folder"
            )

        self.assertIn("Closed app: spotify", result)
        self.assertIn("Opened folder: downloads", result)
        self.assertEqual(mock_run.call_count, 2)

    def test_opt_in_cloud_mode_sends_only_sanitized_aliases_via_keychain(self) -> None:
        self.controller.cloud_mode_enabled = True

        mock_keyring = MagicMock()
        mock_keyring.get_password.return_value = "AIzaFakeSecretKey12345"

        mock_client = MagicMock()
        mock_client.models.generate_content.return_value = SimpleNamespace(
            function_calls=[
                SimpleNamespace(
                    name="arrange_windows",
                    args={"left": "canva", "right": "youtube", "layout": "top_bottom"},
                )
            ]
        )
        mock_genai = MagicMock()
        mock_genai.Client.return_value = mock_client

        with patch.dict(
            "sys.modules",
            {
                "keyring": mock_keyring,
                "google": MagicMock(genai=mock_genai),
                "google.genai": mock_genai,
                "google.genai.types": MagicMock(),
            },
        ), patch("subprocess.run"), \
             patch("pathlib.Path.exists", return_value=True), \
             patch.object(self.executor, "_get_desktop_bounds", return_value=(0, 28, 1440, 900)):
            result = self.controller.process_transcript(
                "arrange canva and youtube stacked top bottom"
            )

        self.assertEqual(result, "Tiled canva and youtube (top_bottom)")
        sent_contents = mock_client.models.generate_content.call_args.kwargs["contents"]
        self.assertEqual(sent_contents, "arrange [canva] and [youtube] stacked [layout:top_bottom]")


if __name__ == "__main__":
    unittest.main()