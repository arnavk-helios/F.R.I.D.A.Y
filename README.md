# F.R.I.D.A.Y. — Privacy-First Voice OS Agent for macOS

**F.R.I.D.A.Y.** is a local-first, security-hardened desktop voice assistant for macOS (Apple Silicon). Activated offline by **"Hey Friday"**, it pairs RAM-only audio capture and local neural speech models with a three-tier hybrid reasoning brain and a fail-closed macOS execution sandbox.

---

## Core Architecture

### 1. Offline Ears & Hardware Kill-Switch (`core/audio_engine.py`, `ui/`)
* **Zero Disk Audio Persistence:** `sounddevice` captures 16 kHz mono audio in 80 ms frames strictly inside an in-memory ring buffer—raw audio is never written to disk.
* **Custom Wake-Word Detection:** Runs a custom-trained `hey_friday.onnx` model via `openWakeWord`, verified at startup against a SHA-256 integrity manifest (`config/model_hashes.json`) to block tampered model weights.
* **Local Speech-to-Text:** Transcribes spoken commands offline on Apple Silicon unified memory using `mlx-whisper`.
* **Kernel-Level Hard Kill-Switch:** Toggling **Hard Off** in the `rumps` menu bar (`🟢` / `🔵` / `🔴`) or clicking **KILL MIC** on the floating AppKit HUD immediately calls `stream.stop()`, `stream.close()`, and wipes the RAM audio buffer.

### 2. Three-Tier Hybrid Command Brain (`core/local_parser.py`, `core/sanitizer.py`, `core/brain.py`)
* **Tier 1 — Deterministic Local Parser (< 1 ms, Zero Network):** Matches standard open, close, web search, system control, and split-screen workspace commands directly against your allowlist.
* **Tier 2 — Pre-Cloud PII Gate + Local Apple Silicon LLM:** Multi-step or natural-language commands pass through `PrivacySanitizer` (scrubbing file paths, emails, numbers, and personal names into bracketed alias tokens) before routing to a local quantized `Qwen2.5-3B-Instruct-4bit` model via `mlx-lm`.
* **Tier 3 — Opt-In Stateless Cloud Routing:** Disabled by default. When explicitly enabled after a privacy disclosure, only PII-scrubbed alias tokens are sent to `gemini-2.5-flash` using an API key stored in **macOS Keychain** (`keyring`).

### 3. Fail-Closed Execution Sandbox (`core/executor.py`, `core/voice.py`)
* **Strict Schema Validation:** Every tool call is validated through Pydantic v2 models (`ConfigDict(extra="forbid", strict=True)`)—unrecognized tools or extra parameters fail closed.
* **Permission-Locked Allowlist:** `config/aliases.json` enforces `0600` owner-only file permissions, verifies `.app` bundle identifiers, and prevents folder traversal outside the user's home directory.
* **URL & Network Hardening:** Enforces `https://` only, normalizes IDN/punycode hostnames, blocks userinfo (`@`), non-443 ports, and private/loopback IP ranges, and prompts for confirmation on unbookmarked domains.
* **Protected System Bundles & Confirmation Gates:** Critical macOS apps (`Finder`, `Terminal`, `iTerm2`, `System Settings`) are hardcoded as unquittable (`config/protected_bundles.json`), while destructive actions (`close_app`, `close_all_apps`) require explicit HUD confirmation. All subprocesses execute with `shell=False` and parameterized AppleScript (`on run argv`).
* **Non-Blocking Voice Feedback:** `FridayVoice` speaks concise confirmations asynchronously via macOS `say` and interrupts immediately if the wake word triggers again.

---

## Project Structure

```text
vayu-agent/
├── main.py                        # Entry point (menu bar daemon, --listen, --cmd CLI)
├── config/
│   ├── aliases.example.json       # Sanitized template for apps, folders, bookmarks, modes
│   ├── model_hashes.json          # SHA-256 integrity manifest for local ONNX wake models
│   └── protected_bundles.json     # Protected macOS bundle IDs blocked from quitting
├── core/
│   ├── audio_engine.py            # RAM-only mic stream, kill-switch, openWakeWord, mlx-whisper
│   ├── brain.py                   # Multi-clause router, local mlx-lm & opt-in Gemini brain
│   ├── executor.py                # Pydantic v2 validator & sandboxed macOS/AppleScript runner
│   ├── local_parser.py            # Sub-millisecond deterministic command parser
│   ├── sanitizer.py               # Regex PII scrubber & alias tokenizer
│   └── voice.py                   # Non-blocking macOS speech synthesizer
├── ui/
│   ├── hud_overlay.py             # Native macOS AppKit floating HUD & confirmation prompts
│   └── menubar.py                 # rumps status bar controller & state machine
├── tests/                         # Automated security, audio teardown, UI & PII unit tests
├── requirements.in                # Top-level dependencies
├── requirements.txt               # Locked dependency versions
└── THREAT_MODEL.md                # Security boundaries and threat mitigations
```

---

## Setup & Installation

### 1. Prerequisites
* macOS on Apple Silicon (M1/M2/M3/M4) with Python 3.11+
* Homebrew PortAudio (`brew install portaudio`)

### 2. Environment & Dependencies
```bash
git clone [https://github.com/arnavk-helios/Vayu.git](https://github.com/arnavk-helios/Vayu.git)
cd Vayu
python3 -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
```

### 3. Configure Private Allowlist & Wake-Word Model
1. Copy the sanitized example config and lock its file permissions to `0600`:
   ```bash
   cp config/aliases.example.json config/aliases.json
   chmod 600 config/aliases.json
   ```
2. Place your trained `hey_friday.onnx` and `hey_friday.onnx.data` files inside `models/` and register their SHA-256 checksums in `config/model_hashes.json`.