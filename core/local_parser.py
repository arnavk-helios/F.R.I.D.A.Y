import difflib
import re
from typing import Any


class DeterministicLocalParser:
    """Tier 1 zero-network command parser matching transcripts against aliases.json."""

    DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+(/[^\s]*)?$")

    def __init__(self, aliases_config: dict[str, Any]) -> None:
        self.config = aliases_config
        self.target_lookup = self._build_target_lookup()
        self.app_lookup = self._build_app_lookup()
        self.mode_lookup = self._build_mode_lookup()

    def _build_target_lookup(self) -> dict[str, str]:
        lookup: dict[str, str] = {}
        for section in ("apps", "folders", "bookmarks"):
            for alias, entry in self.config.get(section, {}).items():
                lookup[alias.lower()] = alias
                for name in entry.get("spoken_names", []):
                    lookup[name.lower().strip()] = alias
        return lookup

    def _build_app_lookup(self) -> dict[str, str]:
        lookup: dict[str, str] = {}
        for alias, entry in self.config.get("apps", {}).items():
            lookup[alias.lower()] = alias
            for name in entry.get("spoken_names", []):
                lookup[name.lower().strip()] = alias
        return lookup

    def _build_mode_lookup(self) -> dict[str, str]:
        lookup: dict[str, str] = {}
        for mode_key, entry in self.config.get("modes", {}).items():
            lookup[mode_key.lower().replace("_", " ")] = mode_key
            for name in entry.get("spoken_names", []):
                lookup[name.lower().strip()] = mode_key
        return lookup

    @staticmethod
    def _resolve_alias(raw_phrase: str, lookup: dict[str, str]) -> str | None:
        """Resolves exact spoken names first, falling back to high-confidence fuzzy matches."""
        if raw_phrase in lookup:
            return lookup[raw_phrase]
        # Never fuzzy-match domain names (e.g., reddit.com) against local aliases
        if "." in raw_phrase or len(raw_phrase) < 4:
            return None
        matches = difflib.get_close_matches(raw_phrase, lookup.keys(), n=1, cutoff=0.78)
        if matches:
            return lookup[matches[0]]
        return None

    @staticmethod
    def _normalize_transcript(raw_text: str) -> str:
        text = raw_text.strip().lower()
        text = re.sub(r"^(?:hey\s+(?:vayu|jarvis)|vayu|jarvis)[,\s]*", "", text)
        text = re.sub(r"\b(?:please|can you|could you|for me)\b", "", text)
        text = re.sub(r"[.?!,]+$", "", text)
        return re.sub(r"\s+", " ", text).strip()

    def parse(self, raw_transcript: str) -> dict[str, Any] | None:
        """Returns a structured tool call dict, or None if LLM fallback is needed."""
        text = self._normalize_transcript(raw_transcript)
        if not text:
            return None

        # 1. Named modes (e.g. "study mode", "switch to code mode")
        mode_match = re.match(r"^(?:switch to|activate|start|open|enable)?\s*(.+)$", text)
        if mode_match:
            candidate_mode = mode_match.group(1).strip()
            resolved_mode = self._resolve_alias(candidate_mode, self.mode_lookup)
            if resolved_mode:
                return {
                    "tool": "arrange_windows",
                    "args": {"mode": resolved_mode},
                }

        # 2. Split screen commands (e.g. "split youtube and notion", "split screen vscode and github 70 30")
        split_match = re.match(
            r"^(?:split screen|split|tile)\s+(?:between\s+)?(.+?)\s+(?:and|with)\s+(.+?)(?:\s+(70[\s/]30|30[\s/]70|50[\s/]50|top[\s_-]bottom))?$",
            text,
        )
        if split_match:
            left_raw = split_match.group(1).strip()
            right_raw = split_match.group(2).strip()
            layout_raw = split_match.group(3)

            left_alias = self._resolve_alias(left_raw, self.target_lookup)
            right_alias = self._resolve_alias(right_raw, self.target_lookup)
            if left_alias and right_alias:
                layout_map = {
                    None: "50_50",
                    "50 50": "50_50",
                    "50/50": "50_50",
                    "70 30": "70_30",
                    "70/30": "70_30",
                    "30 70": "30_70",
                    "30/70": "30_70",
                    "top bottom": "top_bottom",
                    "top-bottom": "top_bottom",
                    "top_bottom": "top_bottom",
                }
                return {
                    "tool": "arrange_windows",
                    "args": {
                        "left": left_alias,
                        "right": right_alias,
                        "layout": layout_map.get(layout_raw, "50_50"),
                    },
                }
            return None

        # 3. Close / quit commands (always starts with confirmed=False)
        close_match = re.match(r"^(?:close|quit|exit)\s+(.+)$", text)
        if close_match:
            target_raw = close_match.group(1).strip()
            app_alias = self._resolve_alias(target_raw, self.app_lookup)
            if app_alias:
                return {
                    "tool": "close_app",
                    "args": {"app_alias": app_alias, "confirmed": False},
                }
            return None

        # 4. Open commands (allowlisted alias or bare domain flagged for confirmation)
        open_match = re.match(r"^(?:open|launch|start|go to|visit)\s+(.+)$", text)
        if open_match:
            target_raw = open_match.group(1).strip()
            resolved_target = self._resolve_alias(target_raw, self.target_lookup)
            if resolved_target:
                return {
                    "tool": "open_target",
                    "args": {"target_alias": resolved_target},
                }

            cleaned_domain = re.sub(r"^https?://", "", target_raw)
            if self.DOMAIN_RE.match(cleaned_domain):
                return {
                    "tool": "open_website",
                    "args": {"url": f"https://{cleaned_domain}", "confirmed": False},
                }

        return None