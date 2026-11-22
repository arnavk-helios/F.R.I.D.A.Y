import datetime
import difflib
import ipaddress
import json
import os
import stat
import subprocess
from pathlib import Path
from typing import Annotated, Any, Literal
from urllib.parse import quote_plus, urlparse

from pydantic import BaseModel, ConfigDict, Field, ValidationError

LayoutType = Literal["50_50", "70_30", "30_70", "top_bottom"]
SystemActionType = Literal[
    "volume_up",
    "volume_down",
    "set_volume",
    "mute",
    "unmute",
    "dark_mode_toggle",
    "screenshot",
    "minimize_all",
]


class SecurityViolationError(PermissionError):
    """Raised when a command violates Vayu's execution or privacy boundaries."""


class ConfirmationRequiredError(Exception):
    """Raised when an action is valid but requires explicit HUD/voice confirmation."""

    def __init__(self, prompt: str, action_payload: dict[str, Any]) -> None:
        super().__init__(prompt)
        self.prompt = prompt
        self.action_payload = action_payload


class OpenTargetArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    target_alias: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")


class OpenInstalledAppArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    app_name: str = Field(min_length=1, max_length=100)


class OpenWebsiteArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    url: str = Field(min_length=10, max_length=2048)
    confirmed: bool = False


class WebSearchArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    query: str = Field(min_length=1, max_length=300)
    engine: Literal["google", "youtube"] = "google"


class CloseAppArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    app_alias: str = Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")
    confirmed: bool = False


class CloseAllAppsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    except_aliases: list[
        Annotated[str, Field(min_length=1, max_length=64, pattern=r"^[a-z0-9_]+$")]
    ] = Field(default_factory=list)
    confirmed: bool = False


class ArrangeWindowsArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    mode: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$")
    left: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$")
    right: str | None = Field(default=None, pattern=r"^[a-z0-9_]+$")
    layout: LayoutType = "50_50"


class SystemControlArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    action: SystemActionType
    value: int | None = Field(default=None, ge=0, le=100)


class ReplyArgs(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True)
    message: str = Field(min_length=1, max_length=600)
    speak: bool = True


class SandboxedExecutor:
    def __init__(
        self,
        aliases_path: Path = Path("config/aliases.json"),
        protected_path: Path = Path("config/protected_bundles.json"),
        enforce_permissions: bool = True,
    ) -> None:
        self.aliases_path = aliases_path
        self.protected_bundles = self._load_protected_bundles(protected_path)
        self.config = self._load_secure_aliases(aliases_path, enforce_permissions)
        self.saved_bookmark_hosts = self._extract_bookmark_hosts()

    @staticmethod
    def _load_protected_bundles(path: Path) -> set[str]:
        if not path.exists():
            raise FileNotFoundError(f"Missing protected bundle list: {path}")
        data = json.loads(path.read_text(encoding="utf-8"))
        bundles = set(data.get("protected_bundle_ids", []))
        if not bundles:
            raise ValueError("Protected bundle list cannot be empty.")
        return bundles

    @staticmethod
    def _load_secure_aliases(path: Path, enforce_permissions: bool) -> dict[str, Any]:
        if not path.exists():
            raise FileNotFoundError(f"Missing allowlist config: {path}")

        if enforce_permissions:
            mode = stat.S_IMODE(os.stat(path).st_mode)
            if mode != 0o600:
                raise SecurityViolationError(
                    f"Insecure permissions on {path}: expected 0600, got {oct(mode)}. "
                    f"Run: chmod 600 {path}"
                )

        data = json.loads(path.read_text(encoding="utf-8"))
        home_dir = Path.home().resolve()

        # Validate app targets are real .app bundles
        for alias, entry in data.get("apps", {}).items():
            app_path = Path(os.path.expanduser(entry["path"])).resolve()
            if app_path.suffix != ".app":
                raise SecurityViolationError(f"App alias '{alias}' must point to a .app bundle.")

        # Validate folder targets stay inside user's home directory
        for alias, entry in data.get("folders", {}).items():
            folder_path = Path(os.path.expanduser(entry["path"])).resolve()
            if home_dir not in folder_path.parents and folder_path != home_dir:
                raise SecurityViolationError(
                    f"Folder alias '{alias}' escapes user home directory."
                )

        return data

    def _extract_bookmark_hosts(self) -> set[str]:
        hosts: set[str] = set()
        for entry in self.config.get("bookmarks", {}).values():
            parsed = urlparse(entry["url"])
            if parsed.hostname:
                ascii_host = parsed.hostname.encode("idna").decode("ascii").lower()
                hosts.add(ascii_host)
        return hosts

    def validate_and_normalize_url(self, raw_url: str) -> tuple[str, bool]:
        """Validates HTTPS URLs, normalizes punycode, blocks local IPs, and flags unbookmarked hosts."""
        parsed = urlparse(raw_url.strip())
        if parsed.scheme != "https" or not parsed.hostname:
            raise SecurityViolationError("Only explicit HTTPS URLs with valid hostnames are allowed.")
        if parsed.username or parsed.password or "@" in parsed.netloc:
            raise SecurityViolationError("URLs containing userinfo (@) are blocked.")
        if parsed.port not in (None, 443):
            raise SecurityViolationError("Non-standard HTTPS ports are blocked.")

        try:
            ascii_host = parsed.hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise SecurityViolationError("Invalid IDN/punycode hostname.") from exc

        try:
            ip = ipaddress.ip_address(ascii_host)
            if (
                ip.is_private
                or ip.is_loopback
                or ip.is_link_local
                or ip.is_multicast
                or ip.is_reserved
            ):
                raise SecurityViolationError("Private, loopback, or reserved IP targets are blocked.")
        except ValueError as err:
            if "blocked" in str(err):
                raise

        clean_url = parsed._replace(netloc=ascii_host).geturl()
        requires_confirm = ascii_host not in self.saved_bookmark_hosts
        return clean_url, requires_confirm

    def dispatch(self, tool_name: str, raw_args: dict[str, Any]) -> str:
        """Single fail-closed entry point for all local and LLM-routed tool calls."""
        try:
            if tool_name == "open_target":
                args = OpenTargetArgs.model_validate(raw_args)
                return self._open_target(args.target_alias)

            if tool_name == "open_installed_app":
                args = OpenInstalledAppArgs.model_validate(raw_args)
                return self._open_installed_app(args.app_name)

            if tool_name == "open_website":
                args = OpenWebsiteArgs.model_validate(raw_args)
                return self._open_website(args.url, args.confirmed)

            if tool_name == "web_search":
                args = WebSearchArgs.model_validate(raw_args)
                return self._web_search(args.query, args.engine)

            if tool_name == "close_app":
                args = CloseAppArgs.model_validate(raw_args)
                return self._close_app(args.app_alias, args.confirmed)

            if tool_name == "close_all_apps":
                args = CloseAllAppsArgs.model_validate(raw_args)
                return self._close_all_apps(args.except_aliases, args.confirmed)

            if tool_name == "arrange_windows":
                args = ArrangeWindowsArgs.model_validate(raw_args)
                return self._arrange_windows(args)

            if tool_name == "system_control":
                args = SystemControlArgs.model_validate(raw_args)
                return self._system_control(args.action, args.value)

            if tool_name == "reply":
                args = ReplyArgs.model_validate(raw_args)
                return self._reply(args.message, args.speak)

            raise SecurityViolationError(f"Unknown or unallowlisted tool: {tool_name}")

        except ValidationError as exc:
            raise SecurityViolationError("Tool schema validation failed (fail-closed).") from exc

    def _get_running_gui_targets(self) -> list[tuple[str, str]]:
        """Queries macOS NSWorkspace natively for open non-protected GUI apps [(alias_or_name, bundle_id)]."""
        try:
            from AppKit import NSWorkspace
        except ImportError:
            return []

        bundle_to_alias = {
            entry["bundle_id"]: alias
            for alias, entry in self.config.get("apps", {}).items()
            if "bundle_id" in entry
        }
        always_protected = self.protected_bundles | {
            "com.vayu.agent",
            "org.python.python",
        }
        if os.environ.get("TERM_PROGRAM") == "vscode":
            always_protected.add("com.microsoft.VSCode")

        targets: list[tuple[str, str]] = []
        seen_bundles: set[str] = set()

        for app in NSWorkspace.sharedWorkspace().runningApplications():
            if int(app.activationPolicy()) != 0:
                continue
            bundle_id = str(app.bundleIdentifier() or "").strip()
            if not bundle_id or bundle_id in always_protected or bundle_id in seen_bundles:
                continue
            seen_bundles.add(bundle_id)
            proc_name = str(app.localizedName() or bundle_id).strip()
            label = bundle_to_alias.get(bundle_id, proc_name.lower())
            targets.append((label, bundle_id))

        return targets

    def get_running_apps(self) -> list[str]:
        """Provides live non-protected open app context to HybridBrain."""
        return [label for label, _ in self._get_running_gui_targets()]

    def _open_target(self, alias: str) -> str:
        apps = self.config.get("apps", {})
        folders = self.config.get("folders", {})
        bookmarks = self.config.get("bookmarks", {})

        if alias in apps:
            app_path = Path(os.path.expanduser(apps[alias]["path"])).resolve()
            if not app_path.exists():
                raise FileNotFoundError(f"Application bundle not found: {app_path}")
            subprocess.run(["open", str(app_path)], check=True, shell=False)
            return f"Opened app: {alias}"

        if alias in folders:
            folder_path = Path(os.path.expanduser(folders[alias]["path"])).resolve()
            if not folder_path.is_dir():
                raise FileNotFoundError(f"Folder not found: {folder_path}")
            subprocess.run(["open", str(folder_path)], check=True, shell=False)
            return f"Opened folder: {alias}"

        if alias in bookmarks:
            return self._open_website(bookmarks[alias]["url"], confirmed=True)

        raise SecurityViolationError(f"Target '{alias}' is not in aliases.json allowlist.")

    def _open_installed_app(self, app_name: str) -> str:
        """Dynamically discovers and opens any installed macOS .app bundle by name."""
        cleaned = app_name.strip().lower().removesuffix(".app")
        if not cleaned:
            raise SecurityViolationError("Empty application name.")

        # Check allowlist spoken names first
        for alias, entry in self.config.get("apps", {}).items():
            spoken = [alias.lower()] + [s.lower() for s in entry.get("spoken_names", [])]
            if cleaned in spoken:
                return self._open_target(alias)

        search_dirs = [
            Path("/Applications"),
            Path.home() / "Applications",
            Path("/System/Applications"),
            Path("/System/Applications/Utilities"),
        ]
        discovered: dict[str, Path] = {}
        for base_dir in search_dirs:
            if not base_dir.is_dir():
                continue
            for item in base_dir.iterdir():
                if item.suffix == ".app":
                    discovered[item.stem.lower()] = item.resolve()

        target_bundle: Path | None = discovered.get(cleaned)
        if target_bundle is None:
            matches = difflib.get_close_matches(
                cleaned, list(discovered.keys()), n=1, cutoff=0.75
            )
            if matches:
                target_bundle = discovered[matches[0]]

        if target_bundle is None or not target_bundle.exists():
            raise FileNotFoundError(
                f"No installed macOS application matching '{app_name}' found."
            )

        subprocess.run(["open", str(target_bundle)], check=True, shell=False)
        return f"Opened installed app: {target_bundle.stem}"

    def _open_website(self, raw_url: str, confirmed: bool, new_window: bool = False) -> str:
        clean_url, needs_confirm = self.validate_and_normalize_url(raw_url)
        if needs_confirm and not confirmed:
            raise ConfirmationRequiredError(
                prompt=f"Open unbookmarked domain {urlparse(clean_url).hostname}?",
                action_payload={"tool": "open_website", "args": {"url": clean_url, "confirmed": True}},
            )

        browser = self.config["default_browser"]
        browser_bundle = browser["bundle_id"]
        browser_path = str(Path(os.path.expanduser(browser["path"])).resolve())

        if new_window:
            if browser_bundle == "com.apple.Safari":
                script = """
                on run argv
                    set targetUrl to item 1 of argv
                    tell application id "com.apple.Safari"
                        activate
                        make new document with properties {URL:targetUrl}
                    end tell
                end run
                """
                subprocess.run(["osascript", "-e", script, clean_url], check=True, shell=False)
            else:
                subprocess.run(
                    ["open", "-na", browser_path, "--args", "--new-window", clean_url],
                    check=True,
                    shell=False,
                )
        else:
            subprocess.run(["open", "-a", browser_path, clean_url], check=True, shell=False)

        return f"Opened website: {clean_url}"

    def _web_search(self, query: str, engine: Literal["google", "youtube"]) -> str:
        encoded = quote_plus(query.strip())
        if engine == "youtube":
            url = f"https://www.youtube.com/results?search_query={encoded}"
        else:
            url = f"https://www.google.com/search?q={encoded}"

        browser_path = str(
            Path(os.path.expanduser(self.config["default_browser"]["path"])).resolve()
        )
        subprocess.run(["open", "-a", browser_path, url], check=True, shell=False)
        return f"Searched {engine.title()} for '{query.strip()}'"

    def _close_app(self, app_alias: str, confirmed: bool) -> str:
        apps = self.config.get("apps", {})
        if app_alias not in apps:
            raise SecurityViolationError(f"App '{app_alias}' is not in aliases.json allowlist.")

        bundle_id = apps[app_alias]["bundle_id"]
        if bundle_id in self.protected_bundles:
            raise SecurityViolationError(f"Blocked quit on protected bundle: {bundle_id}")

        if not confirmed:
            raise ConfirmationRequiredError(
                prompt=f"Confirm quitting {app_alias}?",
                action_payload={"tool": "close_app", "args": {"app_alias": app_alias, "confirmed": True}},
            )

        applescript = """
        on run argv
            set targetBundle to item 1 of argv
            tell application id targetBundle to quit
        end run
        """
        subprocess.run(["osascript", "-e", applescript, bundle_id], check=True, shell=False)
        return f"Closed app: {app_alias}"

    def _close_all_apps(self, except_aliases: list[str], confirmed: bool) -> str:
        apps = self.config.get("apps", {})
        excluded_bundles: set[str] = set()
        for alias in except_aliases:
            if alias not in apps:
                raise SecurityViolationError(
                    f"Exception app '{alias}' is not in aliases.json allowlist."
                )
            excluded_bundles.add(apps[alias]["bundle_id"])

        targets = [
            (label, bundle_id)
            for label, bundle_id in self._get_running_gui_targets()
            if bundle_id not in excluded_bundles
        ]
        if not targets:
            return "No open non-protected apps to close."

        labels = [label for label, _ in targets]
        if not confirmed:
            raise ConfirmationRequiredError(
                prompt=f"Confirm quitting {len(targets)} open app(s): {', '.join(labels)}?",
                action_payload={
                    "tool": "close_all_apps",
                    "args": {"except_aliases": except_aliases, "confirmed": True},
                },
            )

        applescript = """
        on run argv
            set targetBundle to item 1 of argv
            tell application id targetBundle to quit
        end run
        """
        for _, bundle_id in targets:
            subprocess.run(
                ["osascript", "-e", applescript, bundle_id],
                check=True,
                shell=False,
            )
        return f"Closed {len(targets)} app(s): {', '.join(labels)}"

    def _system_control(self, action: SystemActionType, value: int | None) -> str:
        if action == "set_volume":
            level = 50 if value is None else value
            script = """
            on run argv
                set volLevel to (item 1 of argv) as integer
                set volume output volume volLevel without output muted
            end run
            """
            subprocess.run(["osascript", "-e", script, str(level)], check=True, shell=False)
            return f"Set volume to {level}%"

        if action in ("volume_up", "volume_down"):
            delta = 15 if action == "volume_up" else -15
            script = f"""
            set curVol to output volume of (get volume settings)
            set nextVol to curVol + ({delta})
            if nextVol > 100 then set nextVol to 100
            if nextVol < 0 then set nextVol to 0
            set volume output volume nextVol without output muted
            return nextVol
            """
            res = subprocess.run(
                ["osascript", "-e", script],
                capture_output=True,
                text=True,
                check=True,
                shell=False,
            )
            return f"Volume adjusted to {res.stdout.strip()}%"

        if action == "mute":
            subprocess.run(
                ["osascript", "-e", "set volume with output muted"],
                check=True,
                shell=False,
            )
            return "Muted system audio"

        if action == "unmute":
            subprocess.run(
                ["osascript", "-e", "set volume without output muted"],
                check=True,
                shell=False,
            )
            return "Unmuted system audio"

        if action == "dark_mode_toggle":
            script = 'tell application "System Events" to tell appearance preferences to set dark mode to not dark mode'
            subprocess.run(["osascript", "-e", script], check=True, shell=False)
            return "Toggled macOS Dark Mode"

        if action == "screenshot":
            ts = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
            out_path = Path.home() / "Desktop" / f"Vayu_Screenshot_{ts}.png"
            subprocess.run(["screencapture", "-x", str(out_path)], check=True, shell=False)
            return f"Saved screenshot to Desktop/{out_path.name}"

        if action == "minimize_all":
            script = """
            tell application "System Events"
                set visible of every process whose visible is true and background only is false and bundle identifier is not "com.apple.finder" and bundle identifier is not "com.microsoft.VSCode" and bundle identifier is not "com.apple.Terminal" to false
            end tell
            """
            subprocess.run(["osascript", "-e", script], check=True, shell=False)
            return "Minimized open windows to show Desktop"

        raise SecurityViolationError(f"Unsupported system control: {action}")

    @staticmethod
    def _reply(message: str, speak: bool) -> str:
        clean_msg = message.strip()
        if speak and clean_msg:
            subprocess.Popen(
                ["say", "-r", "195", clean_msg],
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return clean_msg

    def _resolve_split_side(self, alias: str) -> tuple[str, int]:
        """Launches target (isolated browser window if bookmark) and returns (bundle_id, window_index)."""
        apps = self.config.get("apps", {})
        bookmarks = self.config.get("bookmarks", {})
        browser_bundle = self.config["default_browser"]["bundle_id"]

        if alias in apps:
            self._open_target(alias)
            return apps[alias]["bundle_id"], 1

        if alias in bookmarks:
            self._open_website(bookmarks[alias]["url"], confirmed=True, new_window=True)
            return browser_bundle, 1

        raise SecurityViolationError(f"Split-screen target '{alias}' must be an allowlisted app or bookmark.")

    def _arrange_windows(self, args: ArrangeWindowsArgs) -> str:
        if args.mode:
            modes = self.config.get("modes", {})
            if args.mode not in modes:
                raise SecurityViolationError(f"Mode '{args.mode}' is not defined in aliases.json.")
            preset = modes[args.mode]
            left_alias = preset["left"]
            right_alias = preset["right"]
            layout: LayoutType = preset.get("layout", args.layout)
        else:
            if not args.left or not args.right:
                raise SecurityViolationError("Split screen requires either 'mode' or both 'left' and 'right'.")
            left_alias = args.left
            right_alias = args.right
            layout = args.layout

        left_bundle, left_win_idx = self._resolve_split_side(left_alias)
        right_bundle, right_win_idx = self._resolve_split_side(right_alias)

        # If both sides are websites in the same browser, left is now window 2 and right is window 1
        if left_bundle == right_bundle:
            left_win_idx = 2
            right_win_idx = 1

        left_rect, right_rect = self._compute_split_rects(layout)
        self._tile_window(left_bundle, left_win_idx, *left_rect)
        self._tile_window(right_bundle, right_win_idx, *right_rect)
        return f"Tiled {left_alias} and {right_alias} ({layout})"

    def _get_desktop_bounds(self) -> tuple[int, int, int, int]:
        script = """
        tell application "Finder"
            set b to bounds of window of desktop
            return (item 1 of b as text) & "," & (item 2 of b as text) & "," & (item 3 of b as text) & "," & (item 4 of b as text)
        end tell
        """
        result = subprocess.run(
            ["osascript", "-e", script], capture_output=True, text=True, check=True, shell=False
        )
        parts = [int(p.strip()) for p in result.stdout.strip().split(",")]
        return parts[0], max(parts[1], 28), parts[2], parts[3]

    def _compute_split_rects(
        self, layout: LayoutType
    ) -> tuple[tuple[int, int, int, int], tuple[int, int, int, int]]:
        x0, y0, x1, y1 = self._get_desktop_bounds()
        total_w = x1 - x0
        total_h = y1 - y0

        if layout == "top_bottom":
            half_h = total_h // 2
            return (x0, y0, total_w, half_h), (x0, y0 + half_h, total_w, total_h - half_h)

        ratio = {"50_50": 0.5, "70_30": 0.7, "30_70": 0.3}[layout]
        left_w = int(total_w * ratio)
        right_w = total_w - left_w
        return (x0, y0, left_w, total_h), (x0 + left_w, y0, right_w, total_h)

    @staticmethod
    def _tile_window(bundle_id: str, win_index: int, x: int, y: int, width: int, height: int) -> None:
        applescript = """
        on run argv
            set targetBundle to item 1 of argv
            set winIdx to (item 2 of argv) as integer
            set xPos to (item 3 of argv) as integer
            set yPos to (item 4 of argv) as integer
            set winW to (item 5 of argv) as integer
            set winH to (item 6 of argv) as integer

            tell application id targetBundle to activate
            delay 0.25

            tell application "System Events"
                set targetProc to first application process whose bundle identifier is targetBundle
                set frontmost of targetProc to true
                if (count of windows of targetProc) >= winIdx then
                    set targetWin to window winIdx of targetProc
                    set position of targetWin to {xPos, yPos}
                    set size of targetWin to {winW, winH}
                end if
            end tell
        end run
        """
        subprocess.run(
            [
                "osascript",
                "-e",
                applescript,
                bundle_id,
                str(win_index),
                str(x),
                str(y),
                str(width),
                str(height),
            ],
            check=True,
            shell=False,
        )