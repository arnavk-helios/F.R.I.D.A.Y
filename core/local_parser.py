import difflib
import re
from typing import Any


class DeterministicLocalParser:
    """Tier 1 zero-network command parser matching transcripts against aliases.json."""

    DOMAIN_RE = re.compile(r"^[a-z0-9-]+(\.[a-z0-9-]+)+(/[^\s]*)?$")

    LAYOUT_MAP = {
        None: "50_50",
        "5050": "50_50",
        "50 50": "50_50",
        "50-50": "50_50",
        "50/50": "50_50",
        "7030": "70_30",
        "70 30": "70_30",
        "70-30": "70_30",
        "70/30": "70_30",
        "3070": "30_70",
        "30 70": "30_70",
        "30-70": "30_70",
        "30/70": "30_70",
        "topbottom": "top_bottom",
        "top bottom": "top_bottom",
        "top-bottom": "top_bottom",
        "top_bottom": "top_bottom",
    }

    LAYOUT_PATTERN = r"(70[\s/-]?30|30[\s/-]?70|50[\s/-]?50|top[\s_-]?bottom)"

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

    def _resolve_target(self, raw: str) -> str | None:
        cleaned = raw.strip().strip(",").lower()
        if cleaned in self.target_lookup:
            return self.target_lookup[cleaned]
        if "." not in cleaned:
            matches = difflib.get_close_matches(
                cleaned, list(self.target_lookup.keys()), n=1, cutoff=0.78
            )
            if matches:
                return self.target_lookup[matches[0]]
        return None

    def _resolve_app(self, raw: str) -> str | None:
        cleaned = raw.strip().strip(",").lower()
        if cleaned in self.app_lookup:
            return self.app_lookup[cleaned]
        matches = difflib.get_close_matches(
            cleaned, list(self.app_lookup.keys()), n=1, cutoff=0.78
        )
        if matches:
            return self.app_lookup[matches[0]]
        return None

    @staticmethod
    def _normalize_transcript(raw_text: str) -> str:
        text = raw_text.strip().lower()
        text = re.sub(
            r"^(?:hey\s+jarvis|jarvis|hey\s+vayu|vayu|hey\s+friday|friday)[,\s]*", "", text
        )
        text = re.sub(r"\b(?:please|also)\b", "", text)
        text = re.sub(r"[.?!]+$", "", text)
        return re.sub(r"\s+", " ", text).strip()

    def parse(self, raw_transcript: str) -> dict[str, Any] | None:
        """Returns a structured tool call dict, or None if LLM fallback is needed."""
        text = self._normalize_transcript(raw_transcript)
        if not text:
            return None

        # 1. Named modes (e.g. "study mode", "switch to code mode")
        mode_match = re.match(
            r"^(?:switch to|activate|start|open|enable)?\s*(.+)$", text
        )
        if mode_match:
            candidate_mode = mode_match.group(1).strip()
            if candidate_mode in self.mode_lookup:
                return {
                    "tool": "arrange_windows",
                    "args": {"mode": self.mode_lookup[candidate_mode]},
                }

        # 2a. Compound split with split keyword (e.g. "open canva and youtube and split screen 5050")
        compound_split = re.match(
            rf"^(?:open|launch|put)\s+(.+?)\s+(?:and|with|on)\s+(.+?)\s*,?\s*(?:and\s+)?(?:split(?:\s+the)?\s+screen|split|tile|side by side)(?:\s*,?\s*{self.LAYOUT_PATTERN})?$",
            text,
        )
        if compound_split:
            left_alias = self._resolve_target(compound_split.group(1))
            right_alias = self._resolve_target(compound_split.group(2))
            layout_raw = compound_split.group(3)
            if left_alias and right_alias:
                return {
                    "tool": "arrange_windows",
                    "args": {
                        "left": left_alias,
                        "right": right_alias,
                        "layout": self.LAYOUT_MAP.get(layout_raw, "50_50"),
                    },
                }

        # 2b. Shorthand split without "split" keyword (e.g. "open canva and youtube, 5050")
        shorthand_split = re.match(
            rf"^(?:open|launch|put)\s+(.+?)\s+(?:and|with|on)\s+(.+?)\s*,?\s*{self.LAYOUT_PATTERN}$",
            text,
        )
        if shorthand_split:
            left_alias = self._resolve_target(shorthand_split.group(1))
            right_alias = self._resolve_target(shorthand_split.group(2))
            layout_raw = shorthand_split.group(3)
            if left_alias and right_alias:
                return {
                    "tool": "arrange_windows",
                    "args": {
                        "left": left_alias,
                        "right": right_alias,
                        "layout": self.LAYOUT_MAP.get(layout_raw, "50_50"),
                    },
                }

        # 2c. Direct split phrasing (e.g. "split youtube and notion", "split screen between canva and youtube")
        split_match = re.match(
            rf"^(?:split(?:\s+the)?\s+screen|split|tile)(?:\s+between)?\s+(.+?)\s+(?:and|with|on)\s+(.+?)(?:\s*,?\s*{self.LAYOUT_PATTERN})?$",
            text,
        )
        if split_match:
            left_alias = self._resolve_target(split_match.group(1))
            right_alias = self._resolve_target(split_match.group(2))
            layout_raw = split_match.group(3)
            if left_alias and right_alias:
                return {
                    "tool": "arrange_windows",
                    "args": {
                        "left": left_alias,
                        "right": right_alias,
                        "layout": self.LAYOUT_MAP.get(layout_raw, "50_50"),
                    },
                }
            return None

        # 3. Close / quit commands (always starts with confirmed=False)
        close_match = re.match(r"^(?:close|quit|exit)\s+(.+)$", text)
        if close_match:
            app_alias = self._resolve_app(close_match.group(1))
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
            resolved = self._resolve_target(target_raw)
            if resolved:
                return {
                    "tool": "open_target",
                    "args": {"target_alias": resolved},
                }

            cleaned_domain = re.sub(r"^https?://", "", target_raw)
            if self.DOMAIN_RE.match(cleaned_domain):
                return {
                    "tool": "open_website",
                    "args": {"url": f"https://{cleaned_domain}", "confirmed": False},
                }

        return None