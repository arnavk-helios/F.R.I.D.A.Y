# Threat Model — Vayu Voice OS Agent

## 1. Protected Assets
1. **Microphone Audio Stream:** Live room audio captured via `sounddevice`.
2. **Local Filesystem & Processes:** User folders (`~/Downloads`, `~/Documents`, `~/Developer`) and macOS apps.
3. **API Credentials:** Optional Gemini API key stored only in **macOS Keychain** (`keyring`).
4. **Command Transcripts:** Spoken commands transcribed in unified memory without disk logging.

## 2. Trust Boundaries
* **Hardware ↔ RAM Audio Loop:** 16 kHz PCM frames stay strictly in a bounded RAM buffer (`collections.deque`) and are never written to `.wav` files on disk.
* **Device ↔ Optional Cloud API:** Enforced by `core/sanitizer.py`. Un-aliased paths, emails, digits, and personal names fail closed (`PIIDetectedError`) before any network request is made.
* **Intent Router ↔ macOS OS Execution:** Enforced by `core/executor.py`. Both Tier 1 rules and LLM outputs must pass strict Pydantic v2 (`extra="forbid"`) schemas and allowlist checks before calling `subprocess.run(..., shell=False)`.

## 3. Threat Vectors & Mitigations
| Threat Vector | Attack / Failure Scenario | Mitigation |
|---|---|---|
| **Passive Eavesdropping** | Mic remains active while muted in software | `disable_mic()` runs `stream.stop()` and `stream.close()` (stream teardown + OS permission gating) and clears the RAM buffer. |
| **Media / Replay Spoofing** | Background video or speaker says *"Hey Vayu"* | 5-second wake-word cooldown + floating HUD pill with an instant **`KILL MIC`** button. |
| **AppleScript / Shell Injection** | Malicious or misheard text breaks out of `osascript` | Zero string interpolation; all arguments passed via `on run argv` with `shell=False`. |
| **Critical App Termination** | Misheard command quits Finder, Terminal, or Vayu | Hardcoded `config/protected_bundles.json` deny-list + mandatory HUD confirmation (`confirmed=True`) for all `close_app` actions. |
| **URL & Homograph Phishing** | Redirect to `http://`, `file://`, `127.0.0.1`, `user@host`, or IDN lookalike domain | `urllib.parse` enforces `https://`, normalizes IDNA punycode, blocks private/loopback IPs and userinfo (`@`), and prompts for confirmation on unbookmarked domains. |
| **PII Leakage to Cloud LLM** | Spoken file paths, numbers, or names sent to Gemini | `PrivacySanitizer` replaces allowlisted targets with bracketed tokens (`[canva]`) and blocks raw paths, emails, digits, and personal names. |
| **Model / Dependency Tampering** | Modified `.onnx` weights or compromised packages | SHA-256 manifest verification (`config/model_hashes.json`) on startup and SHA-256 hash-pinned `requirements.txt`. |
| **Config Tampering** | Another local account modifies `config/aliases.json` | Fail-closed `stat` check requiring exact `0600` (`chmod 600`) file permissions before loading. |

## 4. Residual Risk & Cloud Free-Tier Limitation
* **Offline Default:** Vayu runs 100% offline by default (`openWakeWord` + `mlx-whisper` + local parser / lazy-loaded `mlx-lm`).
* **Cloud Opt-In Trade-Off:** Enabling Cloud Mode sends sanitized bracketed alias tokens to the Gemini API. Requests sent using a **Gemini API free-tier key** may be logged by Google for product improvement; link a paid billing account in Google AI Studio if zero-training data guarantees are required.