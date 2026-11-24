from __future__ import annotations

import ipaddress
import json
import os
import shutil
import stat
import subprocess
from enum import Enum
from pathlib import Path
from typing import Any, Literal
from urllib.parse import quote_plus, urlsplit, urlunsplit

from pydantic import BaseModel, ConfigDict, Field, ValidationError


class SecurityViolationError(Exception):
    """Raised when a command violates permissions, allowlists, schemas, or URL policies."""


class ConfirmationRequiredError(Exception):
    """Raised when a high-impact action requires explicit user confirmation via the HUD."""

    def __init__(self, prompt: str, action_payload: dict[str, Any]) -> None:
        super().__init__(prompt)
        self.prompt = prompt
        self.action_payload = action_payload


class SystemActionType(str, Enum):
    VOLUME_UP = "volume_up"
    VOLUME_DOWN = "volume_down"
    SET_VOLUME = "set_volume"
    MUTE = "mute"
    UNMUTE = "unmute"
    DARK_MODE_TOGGLE = "dark_mode_toggle"
    SCREENSHOT = "screenshot"
    MINIMIZE_ALL = "minimize_all"
    SLEEP_DISPLAY = "sleep_display"
    LOCK_SCREEN = "lock_screen"


class _StrictArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


class OpenTargetArgs(_StrictArgs):
    target_alias: str = Field(min_length=1, max_length=80)


class OpenInstalledAppArgs(_StrictArgs):
    app_name: str = Field(min_length=1, max_length=80)


class OpenWebsiteArgs(_StrictArgs):
    url: str = Field(min_length=1, max_length=512)
    confirmed: bool = False


class CloseAppArgs(_StrictArgs):
    app_alias: str = Field(min_length=1, max_length=80)
    confirmed: bool = False


class CloseAllAppsArgs(_StrictArgs):
    except_aliases: list[str] = Field(default_factory=list, max_length=25)
    confirmed: bool = False


class ArrangeWindowsArgs(_StrictArgs):
    mode: str | None = Field(default=None, min_length=1, max_length=80)
    left: str | None = Field(default=None, min_length=1, max_length=80)
    right: str | None = Field(default=None, min_length=1, max_length=80)
    layout: Literal["50_50", "70_30", "30_70", "top_bottom"] = "50_50"


class WebSearchArgs(_StrictArgs):
    query: str = Field(min_length=1, max_length=300)
    engine: Literal["google", "youtube"] = "google"


class SystemControlArgs(_StrictArgs):
    action: SystemActionType
    value: int | None = Field(default=None, ge=0, le=100)


class ReplyArgs(_StrictArgs):
    message: str = Field(min_length=1, max_length=1000)
    speak: bool = True


class SandboxedExecutor:
    """
    Zero-arbitrary-shell macOS action executor with strict Pydantic schema validation,
    0600 config permission verification, protected bundle blocking, and URL hardening.
    """

    DEFAULT_PROTECTED_BUNDLES = {
        "com.apple.finder",
        "com.apple.terminal",
        "com.googlecode.iterm2",
        "com.microsoft.vscode",
        "com.vayu.agent",
    }

    APP_SEARCH_DIRS = (
        Path("/Applications"),
        Path.home() / "Applications",
        Path("/System/Applications"),
        Path("/System/Applications/Utilities"),
    )

    def __init__(
        self,
        aliases_path: str | Path = "config/aliases.json",
        protected_path: str | Path = "config/protected_bundles.json",
        enforce_permissions: bool = True,
    ) -> None:
        self.aliases_path = Path(aliases_path)
        self.protected_path = Path(protected_path)
        self.enforce_permissions = enforce_permissions

        self.config: dict[str, Any] = self._load_aliases(self.aliases_path)
        self.protected_bundle_ids: set[str] = self._load_protected_bundles(
            self.protected_path
        )

    def _load_aliases(self, path: Path) -> dict[str, Any]:
        if not path.exists():
            raise SecurityViolationError(f"Missing allowlist configuration: {path}")

        if self.enforce_permissions:
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode != 0o600:
                raise SecurityViolationError(
                    f"Insecure permissions {oct(mode)} on {path}; expected 0o600 (chmod 600)."
                )

        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:
            raise SecurityViolationError(f"Malformed JSON in {path}: {exc}") from exc

        if not isinstance(data, dict):
            raise SecurityViolationError(f"Invalid root object in {path}")
        return data

    def _load_protected_bundles(self, path: Path) -> set[str]:
        protected = set(self.DEFAULT_PROTECTED_BUNDLES)
        if path.exists():
            try:
                raw = json.loads(path.read_text(encoding="utf-8"))
                for bid in raw.get("protected_bundle_ids", []):
                    if isinstance(bid, str) and bid.strip():
                        protected.add(bid.strip().lower())
            except Exception as exc:
                raise SecurityViolationError(
                    f"Malformed protected bundle configuration in {path}: {exc}"
                ) from exc
        return protected

    def dispatch(self, tool_name: str, raw_args: dict[str, Any] | None = None) -> str:
        args = dict(raw_args or {})
        normalized_tool = (tool_name or "").strip().lower()

        if normalized_tool in {"open_target", "open_app", "open_folder"}:
            if set(args.keys()) == {"app_name"}:
                args = {"target_alias": args["app_name"]}
            elif set(args.keys()) == {"folder_name"}:
                args = {"target_alias": args["folder_name"]}
            validated = self._validate(OpenTargetArgs, args)
            return self._open_target(validated.target_alias)

        if normalized_tool == "open_installed_app":
            validated = self._validate(OpenInstalledAppArgs, args)
            return self._open_installed_app(validated.app_name)

        if normalized_tool in {"open_website", "open_url"}:
            validated = self._validate(OpenWebsiteArgs, args)
            return self._open_website(validated.url, confirmed=validated.confirmed)

        if normalized_tool == "close_app":
            if set(args.keys()) <= {"app_name", "confirmed"} and "app_name" in args:
                args["app_alias"] = args.pop("app_name")
            validated = self._validate(CloseAppArgs, args)
            return self._close_app(validated.app_alias, confirmed=validated.confirmed)

        if normalized_tool == "close_all_apps":
            validated = self._validate(CloseAllAppsArgs, args)
            return self._close_all_apps(
                except_aliases=validated.except_aliases,
                confirmed=validated.confirmed,
            )

        if normalized_tool in {"arrange_windows", "split_screen", "run_mode"}:
            if set(args.keys()) == {"mode_name"}:
                args = {"mode": args["mode_name"]}
            validated = self._validate(ArrangeWindowsArgs, args)
            return self._arrange_windows(validated)

        if normalized_tool == "web_search":
            validated = self._validate(WebSearchArgs, args)
            return self._web_search(validated.query, engine=validated.engine)

        if normalized_tool == "system_control":
            validated = self._validate(SystemControlArgs, args)
            return self._system_control(validated.action, value=validated.value)

        if normalized_tool == "reply":
            if set(args.keys()) <= {"text", "speak"} and "text" in args:
                args["message"] = args.pop("text")
            validated = self._validate(ReplyArgs, args)
            return self._reply(validated.message, speak=validated.speak)

        raise SecurityViolationError(f"Unknown or disallowed tool: {tool_name}")

    @staticmethod
    def _validate(model_cls: type[BaseModel], args: dict[str, Any]) -> Any:
        try:
            return model_cls.model_validate(args)
        except ValidationError as exc:
            raise SecurityViolationError(f"Schema validation failed: {exc}") from exc

    def _resolve_entry(
        self, section: str, query: str
    ) -> tuple[str, dict[str, Any]] | None:
        needle = query.strip().lower()
        entries: dict[str, Any] = self.config.get(section, {})
        for key, entry in entries.items():
            if key.lower() == needle or key.lower().replace("_", " ") == needle:
                return key, entry
            spoken = [s.lower().strip() for s in entry.get("spoken_names", [])]
            if needle in spoken:
                return key, entry
        return None

    def _default_browser_bundle_id(self) -> str:
        browser = self.config.get("default_browser", {})
        bundle_id = browser.get("bundle_id", "com.apple.Safari")
        if not isinstance(bundle_id, str) or not bundle_id.strip():
            raise SecurityViolationError("Invalid default_browser bundle_id in config.")
        return bundle_id.strip()

    def _open_target(self, target_alias: str) -> str:
        app_match = self._resolve_entry("apps", target_alias)
        if app_match is not None:
            canonical, entry = app_match
            app_path = Path(os.path.expanduser(entry.get("path", "")))
            if not app_path.exists():
                raise SecurityViolationError(f"Application bundle not found: {app_path}")
            subprocess.run(["open", str(app_path)], check=True, shell=False)
            return f"Opened app: {canonical}"

        folder_match = self._resolve_entry("folders", target_alias)
        if folder_match is not None:
            canonical, entry = folder_match
            folder_path = Path(os.path.expanduser(entry.get("path", "")))
            if not folder_path.exists() or not folder_path.is_dir():
                raise SecurityViolationError(f"Folder path not found: {folder_path}")
            subprocess.run(["open", str(folder_path)], check=True, shell=False)
            return f"Opened folder: {canonical}"

        bm_match = self._resolve_entry("bookmarks", target_alias)
        if bm_match is not None:
            canonical, entry = bm_match
            clean_url, _ = self.validate_and_normalize_url(entry["url"])
            browser_bid = self._default_browser_bundle_id()
            subprocess.run(
                ["open", "-b", browser_bid, clean_url], check=True, shell=False
            )
            return f"Opened website: {canonical}"

        raise SecurityViolationError(f"Target not in allowlist: {target_alias}")

    def _open_installed_app(self, app_name: str) -> str:
        alias_match = self._resolve_entry("apps", app_name)
        if alias_match is not None:
            return self._open_target(alias_match[0])

        needle = app_name.strip().lower().removesuffix(".app")
        if not needle or "/" in needle or "\\" in needle or ".." in needle:
            raise SecurityViolationError(f"Invalid application name: {app_name}")

        for base_dir in self.APP_SEARCH_DIRS:
            if not base_dir.is_dir():
                continue
            for candidate in base_dir.glob("*.app"):
                if candidate.stem.lower() == needle:
                    subprocess.run(["open", str(candidate)], check=True, shell=False)
                    return f"Opened installed app: {candidate.stem}"

        raise SecurityViolationError(f"Installed application not found: {app_name}")

    def _bookmarked_hosts(self) -> set[str]:
        hosts: set[str] = set()
        for entry in self.config.get("bookmarks", {}).values():
            raw_url = str(entry.get("url", "")).strip()
            if not raw_url:
                continue
            parsed = urlsplit(raw_url)
            if parsed.hostname:
                try:
                    ascii_host = (
                        parsed.hostname.encode("idna").decode("ascii").lower().rstrip(".")
                    )
                    hosts.add(ascii_host)
                    hosts.add(ascii_host.removeprefix("www."))
                except UnicodeError:
                    continue
        return hosts

    def validate_and_normalize_url(self, raw_url: str) -> tuple[str, bool]:
        """
        Enforces strict HTTPS, rejects userinfo (@) and private/loopback IPs,
        normalizes IDNA/punycode hostnames, and returns (normalized_url, needs_confirmation).
        """
        candidate = raw_url.strip()
        parsed = urlsplit(candidate)
        if parsed.scheme.lower() != "https":
            raise SecurityViolationError(
                f"Blocked non-HTTPS URL scheme: {parsed.scheme!r}"
            )
        if (
            parsed.username is not None
            or parsed.password is not None
            or "@" in parsed.netloc
        ):
            raise SecurityViolationError("Blocked URL containing userinfo credentials.")

        hostname = parsed.hostname
        if not hostname:
            raise SecurityViolationError("Blocked URL with missing hostname.")

        try:
            ascii_host = hostname.encode("idna").decode("ascii").lower().rstrip(".")
        except UnicodeError as exc:
            raise SecurityViolationError(f"Invalid IDNA hostname: {hostname}") from exc

        if not ascii_host or ascii_host == "localhost" or ascii_host.endswith(".local"):
            raise SecurityViolationError(f"Blocked local hostname: {ascii_host}")

        try:
            ip_obj = ipaddress.ip_address(ascii_host)
            if (
                ip_obj.is_loopback
                or ip_obj.is_private
                or ip_obj.is_link_local
                or ip_obj.is_reserved
                or ip_obj.is_multicast
                or ip_obj.is_unspecified
            ):
                raise SecurityViolationError(
                    f"Blocked non-public IP address: {ascii_host}"
                )
        except ValueError:
            if "." not in ascii_host:
                raise SecurityViolationError(f"Blocked single-label host: {ascii_host}")

        netloc = f"{ascii_host}:{parsed.port}" if parsed.port else ascii_host
        normalized_url = urlunsplit(
            ("https", netloc, parsed.path or "", parsed.query, parsed.fragment)
        )
        known_hosts = self._bookmarked_hosts()
        is_bookmarked = (
            ascii_host in known_hosts
            or ascii_host.removeprefix("www.") in known_hosts
        )
        return normalized_url, not is_bookmarked

    def _open_website(self, url_or_alias: str, confirmed: bool = False) -> str:
        bm_match = self._resolve_entry("bookmarks", url_or_alias)
        if bm_match is not None:
            canonical, entry = bm_match
            clean_url, _ = self.validate_and_normalize_url(entry["url"])
            browser_bid = self._default_browser_bundle_id()
            subprocess.run(
                ["open", "-b", browser_bid, clean_url], check=True, shell=False
            )
            return f"Opened website: {canonical}"

        candidate = url_or_alias.strip()
        if ":" not in candidate:
            candidate = f"https://{candidate}"

        clean_url, needs_confirm = self.validate_and_normalize_url(candidate)
        if needs_confirm and not confirmed:
            raise ConfirmationRequiredError(
                prompt=f"Open unbookmarked website {clean_url}?",
                action_payload={
                    "tool": "open_website",
                    "args": {"url": clean_url, "confirmed": True},
                },
            )

        browser_bid = self._default_browser_bundle_id()
        subprocess.run(["open", "-b", browser_bid, clean_url], check=True, shell=False)
        return f"Opened website: {clean_url}"

    def _close_app(self, app_alias: str, confirmed: bool = False) -> str:
        resolved = self._resolve_entry("apps", app_alias)
        if resolved is None:
            raise SecurityViolationError(f"Application not in allowlist: {app_alias}")

        canonical_name, entry = resolved
        bundle_id = str(entry.get("bundle_id", "")).strip()
        if not bundle_id:
            raise SecurityViolationError(f"Missing bundle_id for {canonical_name}")

        if bundle_id.lower() in self.protected_bundle_ids:
            raise SecurityViolationError(
                f"Blocked attempt to quit protected bundle: {bundle_id}"
            )

        if not confirmed:
            raise ConfirmationRequiredError(
                prompt=f"Quit {canonical_name} ({bundle_id})?",
                action_payload={
                    "tool": "close_app",
                    "args": {"app_alias": canonical_name, "confirmed": True},
                },
            )

        quit_script = (
            "on run argv\n"
            "  set targetBundleId to item 1 of argv\n"
            "  tell application id targetBundleId to quit\n"
            "end run"
        )
        subprocess.run(
            ["osascript", "-e", quit_script, bundle_id],
            check=True,
            shell=False,
        )
        return f"Closed app: {canonical_name}"

    def _close_all_apps(
        self, except_aliases: list[str] | None = None, confirmed: bool = False
    ) -> str:
        from AppKit import NSWorkspace

        exempt_bundles = set(self.protected_bundle_ids)
        for alias in except_aliases or []:
            resolved = self._resolve_entry("apps", alias)
            if resolved and resolved[1].get("bundle_id"):
                exempt_bundles.add(str(resolved[1]["bundle_id"]).strip().lower())

        targets: list[tuple[str, str]] = []
        for running_app in NSWorkspace.sharedWorkspace().runningApplications():
            if running_app.activationPolicy() != 0:
                continue
            bid = str(running_app.bundleIdentifier() or "").strip()
            if not bid or bid.lower() in exempt_bundles:
                continue
            name = str(running_app.localizedName() or bid)
            targets.append((name, bid))

        if not targets:
            return "No non-protected GUI apps are currently open."

        if not confirmed:
            names_preview = ", ".join(name for name, _ in targets)
            raise ConfirmationRequiredError(
                prompt=f"Quit all open apps ({names_preview})?",
                action_payload={
                    "tool": "close_all_apps",
                    "args": {
                        "except_aliases": list(except_aliases or []),
                        "confirmed": True,
                    },
                },
            )

        quit_script = (
            "on run argv\n"
            "  repeat with bid in argv\n"
            "    tell application id (bid as text) to quit\n"
            "  end repeat\n"
            "end run"
        )
        subprocess.run(
            ["osascript", "-e", quit_script, *(bid for _, bid in targets)],
            check=True,
            shell=False,
        )
        closed_names = ", ".join(name for name, _ in targets)
        return f"Closed apps: {closed_names}"

    def _get_desktop_bounds(self) -> tuple[int, int, int, int]:
        """Returns (left, top, right, bottom) screen coordinates via Finder desktop bounds."""
        raw = subprocess.check_output(
            [
                "osascript",
                "-e",
                'tell application "Finder" to get bounds of window of desktop',
            ],
            text=True,
        ).strip()
        parts = [int(p.strip()) for p in raw.split(",")]
        if len(parts) != 4:
            return (0, 25, 1440, 900)
        return (parts[0], max(parts[1], 25), parts[2], parts[3])

    def _launch_split_target(self, target: str) -> tuple[str, str]:
        app_match = self._resolve_entry("apps", target)
        if app_match is not None:
            canonical_name, entry = app_match
            app_path = Path(os.path.expanduser(entry.get("path", "")))
            if not app_path.exists():
                raise SecurityViolationError(f"Application bundle not found: {app_path}")
            bundle_id = str(entry.get("bundle_id", "")).strip()
            if not bundle_id:
                raise SecurityViolationError(f"Missing bundle_id for {canonical_name}")
            subprocess.run(["open", "-b", bundle_id], check=True, shell=False)
            return canonical_name, bundle_id

        bm_match = self._resolve_entry("bookmarks", target)
        if bm_match is not None:
            canonical_name, entry = bm_match
            clean_url, _ = self.validate_and_normalize_url(entry["url"])
            browser_bid = self._default_browser_bundle_id()
            subprocess.run(
                ["open", "-b", browser_bid, clean_url], check=True, shell=False
            )
            return canonical_name, browser_bid

        raise SecurityViolationError(f"Split-screen target not in allowlist: {target}")

    def _arrange_windows(self, args: ArrangeWindowsArgs) -> str:
        if args.mode:
            mode_match = self._resolve_entry("modes", args.mode)
            if mode_match is None:
                raise SecurityViolationError(
                    f"Workspace mode not in allowlist: {args.mode}"
                )
            canonical_mode, mode_cfg = mode_match
            for pre_target in mode_cfg.get("open_before", []):
                self._open_target(pre_target)
            if "left" in mode_cfg and "right" in mode_cfg:
                return self._split_pair(
                    mode_cfg["left"],
                    mode_cfg["right"],
                    layout=mode_cfg.get("layout", "50_50"),
                )
            if "split" in mode_cfg:
                sp = mode_cfg["split"]
                return self._split_pair(
                    sp["left"], sp["right"], layout=sp.get("layout", "50_50")
                )
            outcomes = [
                self.dispatch(step["tool"], step.get("args", {}))
                for step in mode_cfg.get("actions", [])
            ]
            return " | ".join(outcomes) if outcomes else f"Activated mode: {canonical_mode}"

        if not args.left or not args.right:
            raise SecurityViolationError(
                "arrange_windows requires either 'mode' or both 'left' and 'right'."
            )
        return self._split_pair(args.left, args.right, layout=args.layout)

    def _split_pair(
        self,
        left: str,
        right: str,
        layout: Literal["50_50", "70_30", "30_70", "top_bottom"] = "50_50",
    ) -> str:
        left_name, left_bid = self._launch_split_target(left)
        right_name, right_bid = self._launch_split_target(right)

        min_x, min_y, max_x, max_y = self._get_desktop_bounds()
        width = max_x - min_x
        height = max_y - min_y

        if layout == "top_bottom":
            mid_y = min_y + (height // 2)
            l_x, l_y, l_w, l_h = min_x, min_y, width, mid_y - min_y
            r_x, r_y, r_w, r_h = min_x, mid_y, width, max_y - mid_y
        else:
            ratio = {"70_30": 0.7, "30_70": 0.3}.get(layout, 0.5)
            split_x = min_x + int(width * ratio)
            l_x, l_y, l_w, l_h = min_x, min_y, split_x - min_x, height
            r_x, r_y, r_w, r_h = split_x, min_y, max_x - split_x, height

        tile_script = (
            "on run argv\n"
            "  set leftId to item 1 of argv\n"
            "  set rightId to item 2 of argv\n"
            "  set lx to (item 3 of argv) as integer\n"
            "  set ly to (item 4 of argv) as integer\n"
            "  set lw to (item 5 of argv) as integer\n"
            "  set lh to (item 6 of argv) as integer\n"
            "  set rx to (item 7 of argv) as integer\n"
            "  set ry to (item 8 of argv) as integer\n"
            "  set rw to (item 9 of argv) as integer\n"
            "  set rh to (item 10 of argv) as integer\n"
            '  tell application "System Events"\n'
            "    set leftProcs to every process whose bundle identifier is leftId\n"
            "    if (count of leftProcs) > 0 then\n"
            "      tell item 1 of leftProcs\n"
            "        if (count of windows) > 0 then\n"
            "          set position of window 1 to {lx, ly}\n"
            "          set size of window 1 to {lw, lh}\n"
            "        end if\n"
            "      end tell\n"
            "    end if\n"
            "    set rightProcs to every process whose bundle identifier is rightId\n"
            "    if (count of rightProcs) > 0 then\n"
            "      tell item 1 of rightProcs\n"
            "        if (count of windows) > 0 then\n"
            "          set position of window 1 to {rx, ry}\n"
            "          set size of window 1 to {rw, rh}\n"
            "        end if\n"
            "      end tell\n"
            "    end if\n"
            "  end tell\n"
            "end run"
        )
        subprocess.run(
            [
                "osascript",
                "-e",
                tile_script,
                left_bid,
                right_bid,
                str(l_x),
                str(l_y),
                str(l_w),
                str(l_h),
                str(r_x),
                str(r_y),
                str(r_w),
                str(r_h),
            ],
            check=True,
            shell=False,
        )
        return f"Tiled {left_name} and {right_name} ({layout})"

    def _web_search(
        self, query: str, engine: Literal["google", "youtube"] = "google"
    ) -> str:
        encoded = quote_plus(query.strip())
        if engine == "youtube":
            target_url = f"https://www.youtube.com/results?search_query={encoded}"
        else:
            target_url = f"https://www.google.com/search?q={encoded}"

        browser_bid = self._default_browser_bundle_id()
        subprocess.run(["open", "-b", browser_bid, target_url], check=True, shell=False)
        return f"Searched {engine.capitalize()} for: {query.strip()}"

    @staticmethod
    def _system_control(action: SystemActionType, value: int | None = None) -> str:
        if action == SystemActionType.SCREENSHOT:
            out_path = Path.home() / "Desktop" / "vayu_screenshot.png"
            subprocess.run(
                ["screencapture", "-x", str(out_path)], check=True, shell=False
            )
            return f"Saved screenshot to {out_path}"

        if action == SystemActionType.SET_VOLUME:
            level = 50 if value is None else value
            script = f"set volume output volume {level}"
        else:
            scripts = {
                SystemActionType.VOLUME_UP: "set volume output volume ((output volume of (get volume settings)) + 10)",
                SystemActionType.VOLUME_DOWN: "set volume output volume ((output volume of (get volume settings)) - 10)",
                SystemActionType.MUTE: "set volume with output muted",
                SystemActionType.UNMUTE: "set volume without output muted",
                SystemActionType.DARK_MODE_TOGGLE: 'tell application "System Events" to tell appearance preferences to set dark mode to not dark mode',
                SystemActionType.MINIMIZE_ALL: 'tell application "System Events" to set visible of every process whose visible is true and bundle identifier is not "com.apple.finder" to false',
                SystemActionType.SLEEP_DISPLAY: 'do shell script "pmset displaysleepnow"',
                SystemActionType.LOCK_SCREEN: 'tell application "System Events" to keystroke "q" using {control down, command down}',
            }
            script = scripts[action]

        subprocess.run(["osascript", "-e", script], check=True, shell=False)
        return f"Executed system action: {action.value}"

    @staticmethod
    def _reply(message: str, speak: bool = True) -> str:
        clean_msg = message.strip()
        if speak and clean_msg and shutil.which("say") is not None:
            cmd = ["say"]
            try:
                from core.voice import _resolve_friday_voice

                voice_name = _resolve_friday_voice()
                if voice_name:
                    cmd.extend(["-v", voice_name])
            except Exception:
                pass
            cmd.extend(["-r", "195", clean_msg])
            subprocess.Popen(
                cmd,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return clean_msg
