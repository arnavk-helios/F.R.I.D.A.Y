from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class PIIDetectedError(ValueError):
    """Raised when un-aliased PII, paths, emails, or numeric identifiers are detected."""


@dataclass(frozen=True)
class SanitizationResult:
    sanitized_text: str
    referenced_aliases: list[str]


class PrivacySanitizer:
    """
    Tier 2 Pre-Cloud & Pre-LLM Privacy Gate:
      - Normalizes layout ratios (50-50, 5050, 70/30, top bottom) into [layout:...] tokens first.
      - Replaces allowlisted apps, folders, and bookmarks with bracketed [alias] tokens.
      - Blocks raw filesystem paths, emails, numeric identifiers, and un-aliased names.
    """

    WAKE_PREFIX_RE = re.compile(
        r"^(?:hey\s+jarvis|jarvis|hey\s+vayu|vayu|hey\s+friday|friday)[,\s]*", re.IGNORECASE
    )
    PATH_RE = re.compile(
        r"(?:~\/|\/(?:Users|Applications|System|Library|Volumes|tmp|var|etc)\/|[A-Za-z]:\\)",
        re.IGNORECASE,
    )
    EMAIL_RE = re.compile(r"\b[A-Za-z0-9_.+-]+@[A-Za-z0-9-]+\.[A-Za-z0-9-.]+\b")
    URL_RE = re.compile(r"https?://[^\s]+", re.IGNORECASE)
    NUMBER_RE = re.compile(r"\d+")
    CAPITALIZED_WORD_RE = re.compile(r"\b[A-Z][a-z]{2,}\b")

    LAYOUT_PATTERNS: list[tuple[re.Pattern[str], str]] = [
        (re.compile(r"\b50[\s/-]?50\b", re.IGNORECASE), "[layout:50_50]"),
        (re.compile(r"\b70[\s/-]?30\b", re.IGNORECASE), "[layout:70_30]"),
        (re.compile(r"\b30[\s/-]?70\b", re.IGNORECASE), "[layout:30_70]"),
        (re.compile(r"\btop[\s_-]?bottom\b", re.IGNORECASE), "[layout:top_bottom]"),
    ]

    NON_NAME_CAPITALIZED = {
        "open",
        "close",
        "quit",
        "exit",
        "split",
        "tile",
        "put",
        "launch",
        "start",
        "arrange",
        "switch",
        "mode",
        "screen",
        "side",
        "and",
        "also",
        "then",
        "with",
        "the",
        "for",
        "to",
        "my",
        "folder",
        "app",
        "website",
        "youtube",
        "canva",
        "notion",
        "chrome",
        "safari",
        "spotify",
        "claude",
        "github",
        "gmail",
        "code",
        "desktop",
        "downloads",
        "documents",
        "projects",
    }

    def __init__(self, aliases_config: dict[str, Any]) -> None:
        self.config = aliases_config
        self._alias_entries = self._build_alias_entries()

    def _build_alias_entries(self) -> list[tuple[str, str]]:
        """Returns (spoken_phrase, alias_key) sorted longest-first for greedy matching."""
        pairs: list[tuple[str, str]] = []
        for section in ("apps", "folders", "bookmarks", "modes"):
            for alias, entry in self.config.get(section, {}).items():
                pairs.append((alias.replace("_", " ").lower(), alias))
                for spoken in entry.get("spoken_names", []):
                    cleaned = spoken.strip().lower()
                    if cleaned:
                        pairs.append((cleaned, alias))
        pairs.sort(key=lambda item: len(item[0]), reverse=True)
        return pairs

    def sanitize(self, raw_transcript: str) -> SanitizationResult:
        text = self.WAKE_PREFIX_RE.sub("", raw_transcript.strip()).strip()
        if not text:
            raise PIIDetectedError("Blocked before LLM (empty command).")

        # 1. Block raw filesystem paths before any alias replacement
        if self.PATH_RE.search(text):
            raise PIIDetectedError(
                "Blocked before LLM (filesystem path detected). Please rephrase using a saved alias in config/aliases.json."
            )

        # 2. Block email addresses
        if self.EMAIL_RE.search(text):
            raise PIIDetectedError(
                "Blocked before LLM (email address detected). Please rephrase using a saved alias in config/aliases.json."
            )

        # 3. Convert split-screen layout ratios into safe [layout:...] tokens BEFORE numeric checks
        working = text
        for pattern, token in self.LAYOUT_PATTERNS:
            working = pattern.sub(token, working)

        # 4. Replace allowlisted aliases with [alias] tokens (skipping existing [...] tokens)
        referenced: list[str] = []
        for spoken, alias in self._alias_entries:
            word_re = re.compile(rf"(?<!\[)\b{re.escape(spoken)}\b(?!\])", re.IGNORECASE)
            if word_re.search(working):
                working = word_re.sub(f"[{alias}]", working)
                if alias not in referenced:
                    referenced.append(alias)

        # Strip bracketed tokens temporarily to inspect residual un-aliased text
        residual = re.sub(r"\[[^\]]+\]", " ", working)

        # 5. Block raw URLs not matched to a saved bookmark alias
        if self.URL_RE.search(residual):
            raise PIIDetectedError(
                "Blocked before LLM (un-aliased URL detected). Please rephrase using a saved alias in config/aliases.json."
            )

        # 6. Block any remaining numeric identifiers (phone numbers, IDs, street numbers)
        if self.NUMBER_RE.search(residual):
            raise PIIDetectedError(
                "Blocked before LLM (numeric identifier detected). Please rephrase using a saved alias in config/aliases.json."
            )

        # 7. Block capitalized personal names outside [alias] tokens
        words = residual.split()
        for idx, raw_word in enumerate(words):
            cleaned_word = re.sub(r"^[^A-Za-z]+|[^A-Za-z]+$", "", raw_word)
            if not cleaned_word:
                continue
            if idx == 0:
                continue
            if self.CAPITALIZED_WORD_RE.match(cleaned_word):
                if cleaned_word.lower() not in self.NON_NAME_CAPITALIZED:
                    raise PIIDetectedError(
                        "Blocked before LLM (personal name detected). Please rephrase using a saved alias in config/aliases.json."
                    )

        normalized_out = re.sub(r"\s+", " ", working).strip()
        return SanitizationResult(
            sanitized_text=normalized_out,
            referenced_aliases=referenced,
        )
