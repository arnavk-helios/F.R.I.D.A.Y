# Vayu — Privacy-First Voice OS Agent for macOS

Vayu is a local-first macOS voice assistant engineered with hardware-level microphone stream teardown, pre-cloud PII filtering, and a fail-closed Pydantic execution sandbox.

## Architecture
1. **Tier 1 — Deterministic Local Parser (`core/local_parser.py`):** Resolves `open`, `close`, `split`, and named workspace modes locally in `< 1 ms`.
2. **Tier 2 — Pre-Cloud PII & Alias Gate (`core/sanitizer.py`):** Replaces allowlisted targets with bracketed tokens (`[canva]`, `[youtube]`) and blocks file paths, emails, digits, and personal names (`PIIDetectedError`).
3. **Tier 3 — Hybrid Brain (`core/brain.py`):** Defaults to **100% Offline** inference on Apple Silicon via `mlx-lm`, with an opt-in **Sanitized Cloud Mode** (`google-genai` + macOS Keychain).
4. **Sandboxed Executor (`core/executor.py`):** Strict Pydantic v2 (`extra="forbid"`) validation, `chmod 600` allowlist enforcement, HTTPS/punycode URL hardening, protected bundle-ID quit blocks, and `on run argv` AppleScript window tiling.
5. **Menu Bar & Floating HUD (`ui/menubar.py`, `ui/hud_overlay.py`):** Live status bar toggle (`🟢 Active` / `🔵 Listening` / `🔴 Hard Off`) and floating `NSPanel` overlay with an instant `KILL MIC` button.

## Quick Setup (macOS Apple Silicon)
```bash
# 1. Create virtual environment & install hash-pinned dependencies
python3 -m venv .venv && source .venv/bin/activate
brew install portaudio ffmpeg
pip install --require-hashes -r requirements.txt

# 2. Create and lock your local allowlist (excluded from Git)
cp config/aliases.example.json config/aliases.json
chmod 600 config/aliases.json

# 3. Run the 19-test security & audio suite
python3 -m unittest discover -s tests -v

# 4. Launch Vayu in the macOS Menu Bar
python3 main.py