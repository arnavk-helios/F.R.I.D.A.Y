cat << 'EOF' > README.md
# Vayu — Privacy-First Autonomous Voice OS Agent for macOS

Vayu is a local-first, security-hardened voice assistant built for Apple Silicon (`mlx`). It combines offline wake-word detection, RAM-only speech-to-text, deterministic fast-path parsing, and an autonomous local LLM tool planner (`Qwen2.5-3B`) inside a fail-closed macOS execution sandbox.

## Architecture

1. **RAM-Only Audio & Wake-Word Gate (`core/audio_engine.py`)**
   * Streams 16 kHz mono frames into an ephemeral in-memory ring buffer (`sounddevice` + `numpy`) with zero disk writes.
   * Applies adaptive RMS gain normalization for quiet laptop microphones and runs `openWakeWord` (`hey_jarvis`) locally after SHA-256 manifest verification (`config/model_hashes.json`).
   * Transcribes post-wake speech locally on Apple Silicon via `mlx-whisper` (`whisper-small.en-mlx`).
2. **Tiered Hybrid Intelligence (`core/local_parser.py`, `core/sanitizer.py`, `core/brain.py`)**
   * **Tier 1 (< 1 ms):** Deterministic local parser handles single and compound `open`, `close`, `split`, and workspace mode commands with zero LLM overhead.
   * **Tier 2 & 3 (Offline Autonomous Planner):** Novel or multi-step requests pass live `NSWorkspace` running-app state to `mlx-lm` (`mlx-community/Qwen2.5-3B-Instruct-4bit`) to plan multi-tool execution—including dynamic macOS app discovery, Google/YouTube search, system controls (volume, dark mode, screenshots, window minimization), workspace cleanup (`close_all_apps`), and spoken answers (`say`).
   * **Opt-In Cloud Mode:** Disabled by default. When toggled on in the menu bar, `PrivacySanitizer` scrubs PII, paths, numbers, and personal names before sending bracketed alias tokens to `gemini-2.5-flash` via macOS Keychain (`keyring`).
3. **Fail-Closed Execution Sandbox & Menu Bar UI (`core/executor.py`, `ui/`)**
   * Strict Pydantic v2 schemas (`extra="forbid"`), `0600`-locked `config/aliases.json`, `urllib.parse` HTTPS/punycode hardening, `on run argv` AppleScript execution (`shell=False`), and protected system bundle IDs.
   * Native `rumps` menu bar indicator (`🟢 Active` / `🔵 Listening` / `🔴 Hard Off`) and floating AppKit HUD (`NSPanel`) with a one-click **KILL MIC** stream teardown button and confirmation prompts (`NSAlert`).

## Quickstart (macOS Apple Silicon)

```bash
python3 -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt

# Copy sanitized config template and lock permissions
cp config/aliases.example.json config/aliases.json
chmod 600 config/aliases.json

# Download openWakeWord ONNX models & pin SHA-256 hashes
python3 -c "import openwakeword; openwakeword.utils.download_models()"
python3 -c '
import hashlib, json, pathlib, openwakeword
models_dir = pathlib.Path(openwakeword.__file__).parent / "resources" / "models"
hashes = {str(p): hashlib.sha256(p.read_bytes()).hexdigest() for p in sorted(models_dir.glob("*.onnx"))}
pathlib.Path("config/model_hashes.json").write_text(json.dumps({"models": hashes}, indent=2))
'

# Run security & pipeline unit tests
python3 -m unittest discover -s tests -v

# Start Vayu with active microphone listening
python3 main.py --listen