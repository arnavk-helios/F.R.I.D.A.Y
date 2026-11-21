from __future__ import annotations

import json
import re
from typing import Any, Callable

from pydantic import BaseModel, ConfigDict, Field

from core.executor import SandboxedExecutor
from core.local_parser import DeterministicLocalParser
from core.sanitizer import PIIDetectedError, PrivacySanitizer


class BrainConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)

    local_model_repo: str = Field(
        default="mlx-community/Qwen2.5-3B-Instruct-4bit", min_length=3
    )
    cloud_model: str = Field(default="gemini-2.5-flash", min_length=3)
    keychain_service: str = Field(default="vayu-agent", min_length=1)
    keychain_account: str = Field(default="gemini_api_key", min_length=1)
    max_local_tokens: int = Field(default=256, ge=64, le=1024)


class HybridBrain:
    """
    Tier 2 & Tier 3 Command Router:
      1. Splits simple multi-clause commands locally with zero LLM calls.
      2. Passes ambiguous/complex intents through PrivacySanitizer (blocks PII/paths).
      3. Routes sanitized alias intents to local mlx-lm (default) or opt-in Gemini API.
      4. Executes every tool call through VayuController's confirmation + Pydantic sandbox.
    """

    CLAUSE_SPLIT_RE = re.compile(r"\s*(?:,|\band then\b|\bthen\b|\band\b)\s*", re.IGNORECASE)

    def __init__(
        self,
        executor: SandboxedExecutor,
        parser: DeterministicLocalParser,
        execute_tool_fn: Callable[[str, dict[str, Any]], str],
        is_cloud_enabled_fn: Callable[[], bool],
        config: BrainConfig | None = None,
    ) -> None:
        self.executor = executor
        self.parser = parser
        self.execute_tool_fn = execute_tool_fn
        self.is_cloud_enabled_fn = is_cloud_enabled_fn
        self.config = config or BrainConfig()
        self.sanitizer = PrivacySanitizer(executor.config)

        self._mlx_model = None
        self._mlx_tokenizer = None

    def route(self, raw_transcript: str) -> str:
        """Main fallback handler attached to VayuController.fallback_router."""
        # Step 1: Fast local multi-clause check (e.g., "open downloads and close spotify")
        local_multi = self._try_local_multi_clause(raw_transcript)
        if local_multi is not None:
            return local_multi

        # Step 2: Mandatory local PII & path gate before any LLM sees the text
        try:
            sanitized = self.sanitizer.sanitize(raw_transcript)
        except PIIDetectedError as pii_err:
            return str(pii_err)

        # Step 3: Route sanitized alias prompt to Opt-In Gemini or Local mlx-lm
        if self.is_cloud_enabled_fn():
            tool_calls = self._infer_cloud_gemini(sanitized.sanitized_text)
        else:
            tool_calls = self._infer_local_mlx(sanitized.sanitized_text)

        if not tool_calls:
            return "Could not map command to any allowlisted action."

        outcomes: list[str] = []
        for call in tool_calls:
            outcomes.append(self.execute_tool_fn(call["tool"], call["args"]))
        return " | ".join(outcomes)

    def _try_local_multi_clause(self, raw_transcript: str) -> str | None:
        """Resolves multi-part commands locally when every clause matches Tier 1 rules."""
        cleaned = re.sub(r"^(?:hey\s+vayu|vayu)[,\s]*", "", raw_transcript.strip(), flags=re.IGNORECASE)
        clauses = [c.strip() for c in self.CLAUSE_SPLIT_RE.split(cleaned) if c.strip()]
        if len(clauses) <= 1:
            return None

        parsed_actions: list[dict[str, Any]] = []
        for clause in clauses:
            action = self.parser.parse(clause)
            if action is None:
                return None  # Contains natural language; escalate to PII gate + LLM
            parsed_actions.append(action)

        outcomes = [
            self.execute_tool_fn(act["tool"], act["args"]) for act in parsed_actions
        ]
        return " | ".join(outcomes)

    def _build_system_prompt(self) -> str:
        apps = sorted(self.executor.config.get("apps", {}).keys())
        folders = sorted(self.executor.config.get("folders", {}).keys())
        bookmarks = sorted(self.executor.config.get("bookmarks", {}).keys())
        modes = sorted(self.executor.config.get("modes", {}).keys())

        return (
            "You are Vayu's strict macOS tool router. Convert sanitized bracketed alias commands "
            "into valid tool calls using ONLY these allowlisted IDs:\n"
            f"- apps: {apps}\n"
            f"- folders: {folders}\n"
            f"- bookmarks: {bookmarks}\n"
            f"- modes: {modes}\n"
            "Available tools:\n"
            '1. open_target(target_alias: str)\n'
            '2. open_website(url: str, confirmed: false)\n'
            '3. close_app(app_alias: str, confirmed: false)\n'
            '4. arrange_windows(mode?: str, left?: str, right?: str, layout: "50_50"|"70_30"|"30_70"|"top_bottom")\n'
            "Always set confirmed=false for close_app and unbookmarked websites. Never invent aliases."
        )

    def _infer_local_mlx(self, sanitized_text: str) -> list[dict[str, Any]]:
        """Runs 100% offline inference on Apple Silicon via mlx-lm."""
        from mlx_lm import generate, load

        if self._mlx_model is None or self._mlx_tokenizer is None:
            self._mlx_model, self._mlx_tokenizer = load(self.config.local_model_repo)

        prompt = (
            f"{self._build_system_prompt()}\n\n"
            'Respond ONLY with a JSON object in the form: '
            '{"tool_calls": [{"tool": "<name>", "args": {...}}]}\n'
            f"Command: {sanitized_text}\nJSON:"
        )
        raw_output = generate(
            self._mlx_model,
            self._mlx_tokenizer,
            prompt=prompt,
            max_tokens=self.config.max_local_tokens,
            temp=0.0,
            verbose=False,
        )
        return self._extract_tool_json(raw_output)

    @staticmethod
    def _extract_tool_json(model_text: str) -> list[dict[str, Any]]:
        match = re.search(r"\{.*\}", model_text, flags=re.DOTALL)
        if not match:
            return []
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []

        calls = payload.get("tool_calls", [])
        valid_calls: list[dict[str, Any]] = []
        for item in calls:
            if isinstance(item, dict) and isinstance(item.get("tool"), str) and isinstance(item.get("args"), dict):
                valid_calls.append({"tool": item["tool"], "args": item["args"]})
        return valid_calls

    def _infer_cloud_gemini(self, sanitized_text: str) -> list[dict[str, Any]]:
        """
        Opt-in stateless Gemini API call using an API key loaded strictly from macOS Keychain.
        Receives ONLY bracketed alias tokens—never raw transcripts or local paths.
        """
        import keyring
        from google import genai
        from google.genai import types

        api_key = keyring.get_password(
            self.config.keychain_service, self.config.keychain_account
        )
        if not api_key:
            raise RuntimeError(
                "Gemini API key not found in macOS Keychain. Store it via: "
                'keyring.set_password("vayu-agent", "gemini_api_key", "<KEY>")'
            )

        try:
            client = genai.Client(api_key=api_key)
            tool_declarations = types.Tool(
                function_declarations=[
                    types.FunctionDeclaration(
                        name="open_target",
                        description="Open an allowlisted app, folder, or saved bookmark alias.",
                        parameters={
                            "type": "OBJECT",
                            "properties": {"target_alias": {"type": "STRING"}},
                            "required": ["target_alias"],
                        },
                    ),
                    types.FunctionDeclaration(
                        name="open_website",
                        description="Open an HTTPS website URL.",
                        parameters={
                            "type": "OBJECT",
                            "properties": {
                                "url": {"type": "STRING"},
                                "confirmed": {"type": "BOOLEAN"},
                            },
                            "required": ["url"],
                        },
                    ),
                    types.FunctionDeclaration(
                        name="close_app",
                        description="Gracefully quit an allowlisted application (always confirmed=false).",
                        parameters={
                            "type": "OBJECT",
                            "properties": {
                                "app_alias": {"type": "STRING"},
                                "confirmed": {"type": "BOOLEAN"},
                            },
                            "required": ["app_alias"],
                        },
                    ),
                    types.FunctionDeclaration(
                        name="arrange_windows",
                        description="Tile two allowlisted apps/bookmarks or activate a named workspace mode.",
                        parameters={
                            "type": "OBJECT",
                            "properties": {
                                "mode": {"type": "STRING"},
                                "left": {"type": "STRING"},
                                "right": {"type": "STRING"},
                                "layout": {
                                    "type": "STRING",
                                    "enum": ["50_50", "70_30", "30_70", "top_bottom"],
                                },
                            },
                        },
                    ),
                ]
            )

            response = client.models.generate_content(
                model=self.config.cloud_model,
                contents=sanitized_text,
                config=types.GenerateContentConfig(
                    system_instruction=self._build_system_prompt(),
                    temperature=0.0,
                    tools=[tool_declarations],
                    automatic_function_calling=types.AutomaticFunctionCallingConfig(
                        disable=True
                    ),
                ),
            )

            calls: list[dict[str, Any]] = []
            for fc in response.function_calls or []:
                calls.append({"tool": fc.name, "args": dict(fc.args or {})})
            return calls

        except Exception as exc:
            # Ensure API key can never appear in logs, HUD pop-ups, or tracebacks
            safe_msg = str(exc).replace(api_key, "[REDACTED_KEY]")
            raise RuntimeError(f"Cloud routing error: {safe_msg}") from None