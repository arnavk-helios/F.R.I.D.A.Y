import json
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from core.executor import (
    ConfirmationRequiredError,
    SandboxedExecutor,
    SecurityViolationError,
)
from core.local_parser import DeterministicLocalParser


class TestPhase1Security(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp_dir = tempfile.TemporaryDirectory()
        self.base = Path(self.tmp_dir.name)

        self.protected_path = self.base / "protected.json"
        self.protected_path.write_text(
            json.dumps({"protected_bundle_ids": ["com.apple.finder", "com.apple.Terminal"]}),
            encoding="utf-8",
        )

        self.aliases_path = self.base / "aliases.json"
        sample_aliases = {
            "default_browser": {
                "bundle_id": "com.google.Chrome",
                "path": "/Applications/Google Chrome.app",
            },
            "apps": {
                "chrome": {
                    "spoken_names": ["chrome"],
                    "bundle_id": "com.google.Chrome",
                    "path": "/Applications/Google Chrome.app",
                },
                "finder": {
                    "spoken_names": ["finder"],
                    "bundle_id": "com.apple.finder",
                    "path": "/System/Library/CoreServices/Finder.app",
                },
                "notion": {
                    "spoken_names": ["notion"],
                    "bundle_id": "notion.id",
                    "path": "/Applications/Notion.app",
                },
            },
            "folders": {
                "downloads": {
                    "spoken_names": ["downloads"],
                    "path": "~/Downloads",
                }
            },
            "bookmarks": {
                "youtube": {
                    "spoken_names": ["youtube"],
                    "url": "https://www.youtube.com",
                }
            },
            "modes": {
                "study_mode": {
                    "spoken_names": ["study mode"],
                    "layout": "50_50",
                    "left": "youtube",
                    "right": "notion",
                }
            },
        }
        self.aliases_path.write_text(json.dumps(sample_aliases), encoding="utf-8")
        os.chmod(self.aliases_path, 0o600)

        self.executor = SandboxedExecutor(
            aliases_path=self.aliases_path,
            protected_path=self.protected_path,
            enforce_permissions=True,
        )
        self.parser = DeterministicLocalParser(self.executor.config)

    def tearDown(self) -> None:
        self.tmp_dir.cleanup()

    def test_rejects_insecure_aliases_permissions(self) -> None:
        os.chmod(self.aliases_path, 0o644)
        with self.assertRaises(SecurityViolationError):
            SandboxedExecutor(
                aliases_path=self.aliases_path,
                protected_path=self.protected_path,
                enforce_permissions=True,
            )

    def test_url_hardening_blocks_http_userinfo_and_loopback(self) -> None:
        for bad_url in [
            "http://example.com",
            "file:///etc/passwd",
            "https://youtube.com@evil.com",
            "https://127.0.0.1/admin",
            "https://192.168.1.1",
        ]:
            with self.assertRaises(SecurityViolationError):
                self.executor.validate_and_normalize_url(bad_url)

    def test_unbookmarked_domain_requires_confirmation(self) -> None:
        parsed = self.parser.parse("Hey Vayu, open reddit.com")
        self.assertIsNotNone(parsed)
        with self.assertRaises(ConfirmationRequiredError):
            self.executor.dispatch(parsed["tool"], parsed["args"])

    def test_protected_bundle_cannot_be_quit_even_when_confirmed(self) -> None:
        with self.assertRaises(SecurityViolationError):
            self.executor.dispatch("close_app", {"app_alias": "finder", "confirmed": True})

    def test_quit_requires_confirmation(self) -> None:
        with self.assertRaises(ConfirmationRequiredError):
            self.executor.dispatch("close_app", {"app_alias": "chrome", "confirmed": False})

    def test_extra_schema_fields_fail_closed(self) -> None:
        with self.assertRaises(SecurityViolationError):
            self.executor.dispatch(
                "open_target",
                {"target_alias": "downloads", "shell_cmd": "rm -rf /"},
            )

    @patch("core.executor.subprocess.run")
    def test_mixed_website_and_app_split_screen(self, mock_run) -> None:
        cmd = self.parser.parse("split youtube and notion")
        self.assertEqual(
            cmd,
            {
                "tool": "arrange_windows",
                "args": {"left": "youtube", "right": "notion", "layout": "50_50"},
            },
        )
        with patch.object(self.executor, "_get_desktop_bounds", return_value=(0, 28, 1440, 900)), \
             patch("pathlib.Path.exists", return_value=True):
            result = self.executor.dispatch(cmd["tool"], cmd["args"])
            self.assertEqual(result, "Tiled youtube and notion (50_50)")


if __name__ == "__main__":
    unittest.main()