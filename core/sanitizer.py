from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any


class PIIDetectedError(PermissionError):
    """Raised when un-aliased PII, paths, or numbers are detected before LLM routing."""

    def __init__(self, category: str) -> None:
        self.category = category
        super().__init__(
            f"Blocked before LLM ({category} detected). "
            "Please rephrase using a saved alias in config/aliases.json."
        )


@dataclass(frozen=True)
class SanitizedPrompt:
    sanitized_text: str
    referenced_aliases: list[str]


class PrivacySanitizer:
    """
    Replaces allowlisted spoken aliases with bracketed tokens (e.g., [canva])
    and blocks raw file paths, emails, digits/phone numbers, and personal names
    from leaving the device.
    """

    PATH_RE = re.compile(r"(?:~|\.{1,2})?/[^\s]+|[a-zA-Z]:\\[^\s]+")
    EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
    DIGIT_RE = re.compile(r"\d+")
    DIRECT_PERSON_RE = re.compile(
        r"\b(?:call|text|email|message|dm|ping|tell|ask)\s+([a-z]{2,})\b"
    )

    SAFE_PRONOUNS_AND_WORDS = {
        "it", "them", "him", "her", "me", "my", "the", "a", "an",
        "on", "up", "in", "out", "off", "back", "now", "please",
    }

    LAYOUT_PATTERNS: tuple[tuple[re.Pattern[str], str], ...] = (
        (re.compile(r"\b70[\s/]30\b"), "[layout:70_30]"),
        (re.compile(r"\b30[\s/]70\b"), "[layout:30_70]"),
        (re.compile(r"\b50[\s/]50\b"), "[layout:50_50]"),
        (re.compile(r"\btop[\s_-]bottom\b"), "[layout:top_bottom]"),
    )

    def __init__(self, aliases_config: dict[str, Any]) -> None:
        self.config = aliases_config
        self._phrase_to_alias, self._alias_regex = self._compile_alias_matcher()
        self._known_alias_tokens = self._collect_known_words()

    def _compile_alias_matcher(self) -> tuple[dict[str, str], re.Pattern[str] | None]:
        pairs: dict[str, str] = {}
        for section in ("apps", "folders", "bookmarks", "modes"):
            for alias_key, entry in self.config.get(section, {}).items():
                pairs[alias_key.lower().replace("_", " ")] = alias_key
                pairs[alias_key.lower()] = alias_key
                for spoken in entry.get("spoken_names", []):
                    cleaned = spoken.strip().lower()
                    if cleaned:
                        pairs[cleaned] = alias_key

        if not pairs:
            return {}, None

        # Single-pass alternation sorted longest-first prevents double-bracketing [[alias]]
        sorted_phrases = sorted(pairs.keys(), key=len, reverse=True)
        alternation = "|".join(re.escape(phrase) for phrase in sorted_phrases)
        pattern = re.compile(rf"(?<!\[)\b(?:{alternation})\b(?!\])", flags=re.IGNORECASE)
        return pairs, pattern

    def _collect_known_words(self) -> set[str]:
        words = {"vayu", "hey", "mac", "os", "ai", "ui", "pdf", "url"}
        for section in ("apps", "folders", "bookmarks", "modes"):
            for alias_key, entry in self.config.get(section, {}).items():
                words.update(alias_key.lower().split("_"))
                for spoken in entry.get("spoken_names", []):
                    words.update(spoken.lower().split())
        return words

    def sanitize(self, raw_transcript: str) -> SanitizedPrompt:
        cleaned = raw_transcript.strip()
        cleaned = re.sub(r"^(?:hey\s+vayu|vayu)[,\s]*", "", cleaned, flags=re.IGNORECASE).strip()
        if not cleaned:
            raise PIIDetectedError("empty command")

        # 1. Check for raw filesystem paths and emails before token replacement
        if self.PATH_RE.search(cleaned):
            raise PIIDetectedError("file path")
        if self.EMAIL_RE.search(cleaned):
            raise PIIDetectedError("email address")

        # 2. Detect capitalized personal names not matching known app/bookmark titles
        words_raw = re.findall(r"\b[A-Za-z]+\b", cleaned)
        for idx, word in enumerate(words_raw):
            if idx == 0:
                continue  # Ignore sentence-initial capitalization
            if word[0].isupper() and word.lower() not in self._known_alias_tokens:
                raise PIIDetectedError("personal name")

        # 3. Single-pass replacement of allowlisted aliases and layout ratios
        tokenized = cleaned.lower()
        referenced: list[str] = []

        if self._alias_regex is not None:
            def _replace_alias(match: re.Match[str]) -> str:
                alias_key = self._phrase_to_alias[match.group(0).lower()]
                if alias_key not in referenced:
                    referenced.append(alias_key)
                return f"[{alias_key}]"

            tokenized = self._alias_regex.sub(_replace_alias, tokenized)

        for layout_re, layout_token in self.LAYOUT_PATTERNS:
            tokenized = layout_re.sub(layout_token, tokenized)

        # 4. Inspect residual un-aliased text for digits or person-targeted verbs
        residual = re.sub(r"\[[^\]]+\]", " ", tokenized)
        if self.DIGIT_RE.search(residual):
            raise PIIDetectedError("numeric identifier")

        person_match = self.DIRECT_PERSON_RE.search(residual)
        if person_match and person_match.group(1) not in self.SAFE_PRONOUNS_AND_WORDS:
            raise PIIDetectedError("personal name")

        normalized_out = re.sub(r"\s+", " ", tokenized).strip()
        return SanitizedPrompt(
            sanitized_text=normalized_out,
            referenced_aliases=referenced,
        )