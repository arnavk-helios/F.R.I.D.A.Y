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
    max_local_tokens: int = Field(default=320, ge=64, le=1024)


class HybridBrain:
    """
    Tier 2 & Tier 3 Autonomous Agent Brain:
      1. Fast-paths exact multi-clause Tier 1 actions locally when unambiguous.
      2. In Cloud Mode (opt-in), enforces strict PrivacySanitizer PII/path scrubbing.
      3. In Offline Mode (100% local), feeds full natural requests + live macOS open-app
         context directly to Qwen2.5-3B so any new app, system control, search, or
         question gets an intelligent plan + spoken/HUD reaction.
    """

    WAKE_PREFIX_RE = re.compile(
        r"^(?:hey\s+jarvis|jarvis|hey\s+vayu|vayu)[,\s]*",
        re.IGNORECASE,
    )
    CLAUSE_SPLIT_RE = re.compile(
        r"\s*(?:,|\band\s+also\b|\balso\b|\band\s+then\b|\bthen\b|\band\b)\s*",
        re.IGNORECASE,
    )

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
        local_multi = self._try_local_multi_clause(raw_transcript)
        if local_multi is not None:
            return local_multi

        if self.is_cloud_enabled_fn():
            # Mandatory pre-cloud PII & path gate before any network request
            try:
                sanitized = self.sanitizer.sanitize(raw_transcript)
            except PIIDetectedError as pii_err:
                return str(pii_err)
            tool_calls = self._infer_cloud_gemini(sanitized.sanitized_text)
        else:
            # 100% offline: pass natural request directly to local Qwen2.5-3B without blocking numbers/names
            cleaned_prompt = self.WAKE_PREFIX_RE.sub("", raw_transcript.strip()).strip()
            if not cleaned_prompt:
                return "I'm listening—what would you like me to do?"
            tool_calls = self._infer_local_mlx(cleaned_prompt)

        print(f"[Vayu LLM Plan] {tool_calls}")

        if not tool_calls:
            return self.execute_tool_fn(
                "reply",
                {
                    "message": "I heard you, but I'm not sure which action to run yet.",
                    "speak": True,
                },
            )

        outcomes: list[str] = []
        for call in tool_calls:
            outcomes.append(self.execute_tool_fn(call["tool"], call["args"]))
        return " | ".join(outcomes)

    def _try_local_multi_clause(self, raw_transcript: str) -> str | None:
        """Resolves multi-part commands locally only when every clause matches Tier 1 rules."""
        cleaned = self.WAKE_PREFIX_RE.sub("", raw_transcript.strip())
        clauses = [c.strip() for c in self.CLAUSE_SPLIT_RE.split(cleaned) if c.strip()]
        if len(clauses) <= 1:
            return None

        parsed_actions: list[dict[str, Any]] = []
        for clause in clauses:
            action = self.parser.parse(clause)
            if action is None:
                return None  # Escalate novel/complex reasoning to LLM
            parsed_actions.append(action)

        outcomes = [
            self.execute_tool_fn(act["tool"], act["args"]) for act in parsed_actions
        ]
        return " | ".join(outcomes)

    def _get_live_open_apps(self) -> list[str]:
        getter = getattr(self.executor, "get_running_apps", None)
        if callable(getter):
            try:
                return list(getter())
            except Exception:
                return []
        return []

    def _build_system_prompt(self) -> str:
        apps = sorted(self.executor.config.get("apps", {}).keys())
        folders = sorted(self.executor.config.get("folders", {}).keys())
        bookmarks = sorted(self.executor.config.get("bookmarks", {}).keys())
        modes = sorted(self.executor.config.get("modes", {}).keys())
        open_now = self._get_live_open_apps()

        return (
            "You are Jarvis (Vayu), an intelligent autonomous macOS desktop assistant. "
            "Reason about the user's intent—even if it is brand new or conversational—and output "
            "an ordered JSON list of tool calls so you ALWAYS react helpfully.\n\n"
            "Live System State:\n"
            f"- Currently open non-protected apps on screen: {open_now}\n\n"
            "Saved Allowlist Aliases:\n"
            f"- apps: {apps}\n"
            f"- folders: {folders}\n"
            f"- bookmarks: {bookmarks}\n"
            f"- modes: {modes}\n\n"
            "Available Tools:\n"
            '1. open_target(target_alias: str) — open a saved alias from apps, folders, or bookmarks.\n'
            '2. open_installed_app(app_name: str) — dynamically find and open ANY macOS app installed on the Mac (e.g., "Calculator", "Notes", "WhatsApp", "Preview") even if not in saved aliases.\n'
            '3. open_website(url: str, confirmed: false) — open a specific https:// website URL.\n'
            '4. web_search(query: str, engine: "google"|"youtube") — search Google or YouTube for any topic.\n'
            '5. close_app(app_alias: str, confirmed: false) — quit one saved app alias.\n'
            '6. close_all_apps(except_aliases: list[str], confirmed: false) — quit ALL currently open non-protected apps, optionally keeping except_aliases open.\n'
            '7. arrange_windows(mode?: str, left?: str, right?: str, layout: "50_50"|"70_30"|"30_70"|"top_bottom") — tile two saved targets or activate a workspace mode.\n'
            '8. system_control(action: "volume_up"|"volume_down"|"set_volume"|"mute"|"unmute"|"dark_mode_toggle"|"screenshot"|"minimize_all", value?: int) — control macOS volume, dark mode, screenshots, or desktop.\n'
            '9. reply(message: str, speak: true) — speak and display a concise, witty, helpful response for general questions, explanations, or requests outside OS actions.\n\n'
            "Reasoning Rules:\n"
            "- Output ONLY valid JSON: {\"tool_calls\": [{\"tool\": \"...\", \"args\": {...}}]}\n"
            "- If the user asks to open an app in saved apps, use open_target. If it's any other Mac app name, use open_installed_app.\n"
            "- If the user asks to search YouTube or Google, use web_search.\n"
            "- If the user asks about volume, mute, dark mode, screenshot, or hiding windows, use system_control.\n"
            "- If the user asks a question, wants advice, or says something conversational, ALWAYS use reply(message=..., speak=true) with a crisp 1-2 sentence answer.\n"
            "- Always set confirmed=false for close_app, close_all_apps, and unbookmarked open_website.\n\n"
            "Examples:\n"
            'Command: open calculator and set volume to 40\n'
            'JSON: {"tool_calls": [{"tool": "open_installed_app", "args": {"app_name": "Calculator"}}, {"tool": "system_control", "args": {"action": "set_volume", "value": 40}}]}\n'
            'Command: search youtube for lofi hip hop beats\n'
            'JSON: {"tool_calls": [{"tool": "web_search", "args": {"query": "lofi hip hop beats", "engine": "youtube"}}]}\n'
            'Command: close everything except chrome\n'
            'JSON: {"tool_calls": [{"tool": "close_all_apps", "args": {"except_aliases": ["chrome"], "confirmed": false}}]}\n'
            'Command: what is the difference between tcp and udp\n'
            'JSON: {"tool_calls": [{"tool": "reply", "args": {"message": "TCP guarantees ordered, reliable delivery with handshakes, while UDP sends packets fast without waiting for confirmation.", "speak": true}}]}'
        )

    def _infer_local_mlx(self, user_text: str) -> list[dict[str, Any]]:
        """Runs 100% offline inference on Apple Silicon via mlx-lm."""
        from mlx_lm import generate, load

        if self._mlx_model is None or self._mlx_tokenizer is None:
            print("[Vayu LLM] Loading local Qwen2.5-3B model into unified memory...")
            self._mlx_model, self._mlx_tokenizer = load(self.config.local_model_repo)

        system_prompt = self._build_system_prompt()
        user_prompt = f"Command: {user_text}\nJSON:"

        if hasattr(self._mlx_tokenizer, "apply_chat_template"):
            messages = [
                {"role": "system", "content": system_prompt},
                {"role": "user", "content": user_prompt},
            ]
            prompt = self._mlx_tokenizer.apply_chat_template(
                messages, tokenize=False, add_generation_prompt=True
            )
        else:
            prompt = (
                f"{system_prompt}\n\n"
                'Respond ONLY with a JSON object in the form: '
                '{"tool_calls": [{"tool": "<name>", "args": {...}}]}\n'
                f"{user_prompt}"
            )

        gen_kwargs: dict[str, Any] = {
            "prompt": prompt,
            "max_tokens": self.config.max_local_tokens,
            "verbose": False,
        }
        try:
            from mlx_lm.sample_utils import make_sampler

            gen_kwargs["sampler"] = make_sampler(temp=0.0)
        except ImportError:
            gen_kwargs["temp"] = 0.0

        raw_output = generate(
            self._mlx_model,
            self._mlx_tokenizer,
            **gen_kwargs,
        )
        calls = self._extract_tool_json(raw_output)
        if calls:
            return calls

        # If model responded in plain text instead of JSON, speak/show it via reply tool
        fallback_text = re.sub(r"```.*?```", "", raw_output, flags=re.DOTALL).strip()
        if fallback_text:
            return [{"tool": "reply", "args": {"message": fallback_text[:500], "speak": True}}]
        return []

    @staticmethod
    def _extract_tool_json(model_text: str) -> list[dict[str, Any]]:
        cleaned = re.sub(r"```(?:json)?|```", "", model_text).strip()
        match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
        if not match:
            return []
        try:
            payload = json.loads(match.group(0))
        except json.JSONDecodeError:
            return []

        calls = payload.get("tool_calls", [])
        valid_calls: list[dict[str, Any]] = []
        for item in calls:
            if (
                isinstance(item, dict)
                and isinstance(item.get("tool"), str)
                and isinstance(item.get("args"), dict)
            ):
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
                        name="open_installed_app",
                        description="Dynamically find and open an installed macOS application by name.",
                        parameters={
                            "type": "OBJECT",
                            "properties": {"app_name": {"type": "STRING"}},
                            "required": ["app_name"],
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
                        name="web_search",
                        description="Search Google or YouTube for a query.",
                        parameters={
                            "type": "OBJECT",
                            "properties": {
                                "query": {"type": "STRING"},
                                "engine": {"type": "STRING", "enum": ["google", "youtube"]},
                            },
                            "required": ["query"],
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
                        name="close_all_apps",
                        description="Gracefully quit all currently open non-protected apps except except_aliases (always confirmed=false).",
                        parameters={
                            "type": "OBJECT",
                            "properties": {
                                "except_aliases": {
                                    "type": "ARRAY",
                                    "items": {"type": "STRING"},
                                },
                                "confirmed": {"type": "BOOLEAN"},
                            },
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
                    types.FunctionDeclaration(
                        name="system_control",
                        description="Control macOS audio volume, dark mode, screenshot, or minimize windows.",
                        parameters={
                            "type": "OBJECT",
                            "properties": {
                                "action": {
                                    "type": "STRING",
                                    "enum": [
                                        "volume_up",
                                        "volume_down",
                                        "set_volume",
                                        "mute",
                                        "unmute",
                                        "dark_mode_toggle",
                                        "screenshot",
                                        "minimize_all",
                                    ],
                                },
                                "value": {"type": "INTEGER"},
                            },
                            "required": ["action"],
                        },
                    ),
                    types.FunctionDeclaration(
                        name="reply",
                        description="Speak and display a concise helpful answer for questions or conversational prompts.",
                        parameters={
                            "type": "OBJECT",
                            "properties": {
                                "message": {"type": "STRING"},
                                "speak": {"type": "BOOLEAN"},
                            },
                            "required": ["message"],
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
            safe_msg = str(exc).replace(api_key, "[REDACTED_KEY]")
            raise RuntimeError(f"Cloud routing error: {safe_msg}") from None