---
name: tts-voice-clone
description: Generate narration in a cloned voice from a short reference audio clip, via the MCC Text to Speech feature (Qwen3-TTS, served locally on the OptiPlex gateway). Use this instead of the plain sherpa-onnx /api/tts voice whenever a specific/consistent voice is needed - e.g. narrating a news flash, a video voiceover, or any script that should sound like a chosen speaker rather than the generic default voice.
---

# TTS Voice Clone

## Overview

Voice cloning text-to-speech via Qwen3-TTS (Apache 2.0, 0.6B), served locally on the OptiPlex gateway and exposed through MCC's Text to Speech page (`/tts`) and its API (`/api/tts/clone`). Given a 3-10 second reference clip of a voice, it narrates arbitrary script text in that same voice.

This is separate from and does not replace the existing `/api/tts` endpoint (`src/lib/server/tts.ts`, sherpa-onnx) - that one is a single fixed voice with no cloning, still fine for quick one-off notifications. Use voice cloning when the output needs to sound like a specific, consistent voice (a news anchor persona, a recurring character, matching a previous narration).

**Architecture:** MCC (Next.js, hosted on the OptiPlex gateway) calls a small FastAPI server on that same box over HTTP (`127.0.0.1:8790` by default, override with `QWEN_TTS_HOST`). Unlike image generation, this is deliberately NOT offloaded to the Mac Mini's GPU: Qwen3-TTS-0.6B is small (~2.5GB) and this is a non-realtime "click generate, wait a bit" workload, so CPU inference on the OptiPlex is fine, and colocating avoids a cross-machine dependency on the Mac Mini being on and reachable. Ollama does not support TTS models at all regardless of host, so unlike image generation this isn't an Ollama call - it's a dedicated server: `scripts/serve_qwen3_tts.py`.

## Bundled Reference Voices

`reference-voices/` holds voice samples that ship with the skill (tracked in git, deployed like any other code) rather than living in `user_files` (live, user-uploaded, not tracked). Use these for a script that always wants the *same* voice on every run - `user_files` entries work too, but a name like `tts-ref-1786080883198-cease.wav` isn't something you'd want to hardcode into a script, and `user_files` content isn't guaranteed to survive someone cleaning it up via File Explorer.

- `st-tng-data-voice.wav` - transcript: `"There is an intense magnetic field, Commander. I am getting an anomalous reading, but it is not strong enough to interpret accurately."`. No longer used by default by either script as of the Welcome greeting/News Flash voice-profile alignment (see below) - kept as a bundled option selectable via a Voice Profile or an explicit `TTS_REFERENCE_AUDIO` override. 7s - over Qwen3-TTS's ~3s recommendation, so cloning quality should be cleaner than the original 1.85s sample it replaced.
- `jeremy-clarkson-this-is-brilliant.mp3` - transcript (auto-transcribed via `/api/tts/transcribe`): `"This is brilliant, but I like this"`. Used by both `speak-news.sh` (News Flash) and `speak.sh` (Welcome greeting) as the final hardcoded fallback voice when no Voice Profile is active, with delivery style `"Slow pace, with a clear pause at the end of every sentence"` by default. 3.2s, right at Qwen3-TTS's ~3s recommendation. The seeded "Jeremy Clarkson" Voice Profile's Content style (see below) is what turns the day's top headlines into a crude Clarkson-style rant before handing that script to this voice - the front-door automation (`~/mcc-data (OPENCLAW_MCC_DATA_ROOT)/homelinq-events.json`) itself now just writes plain, neutral headlines and lets `speak-news.sh` apply the persona. Same `--ephemeral` handling as above - never saved to user_files.

## From a Script (e.g. a news flash)

Call MCC's own API - don't call the Qwen3-TTS server directly, since MCC owns saving outputs to `user_files` and validating the reference file:

```bash
python3 /home/peter/.openclaw/workspace/skills/tts-voice-clone/scripts/generate_cloned_speech.py \
    --reference-audio /home/peter/.openclaw/workspace/skills/tts-voice-clone/reference-voices/st-tng-data-voice.wav \
    --reference-text "Are you able to cease thinking on command?" \
    --text "Here are today's top stories." \
    --style "energetic newsreader tone" \
    --output /tmp/narration.wav
```

`--reference-text` is required - a transcript of what the reference clip actually says (Qwen3-TTS's voice cloning needs this to align on, separate from `--text`, the new script to narrate).

`--reference-audio <local path>` uploads the clip to `user_files` and caches the resulting name locally (keyed on file content, in `.reference-cache.json` next to this script) - a script that runs repeatedly with the same reference file, like a scheduled news flash, reuses that upload instead of creating a fresh duplicate every run. If the cached upload gets deleted (e.g. via File Explorer), the next call re-uploads automatically. Use `--reference-file <name-in-user_files>` instead if you already know the exact uploaded name and want to skip the cache/upload path entirely.

Prints `{"status": "success", "file": "...", "local_path": "..."}` on stdout, or `{"status": "error", ...}` with a non-zero exit code. `--timeout` defaults to 600s - CPU synthesis time scales with script length, so a multi-paragraph briefing needs more headroom than a short test sentence.

### Ephemeral mode (nothing saved to user_files)

Add `--ephemeral` for one-shot playback callers - like the Welcome greeting or a News Flash - that don't want File Explorer accumulating a file per run. Requires `--reference-audio` (not `--reference-file`) and `--output`. The reference clip is sent inline (base64, in the same request) instead of being uploaded first, and MCC's `/api/tts/clone` returns the generated WAV directly in the response body instead of writing it into `user_files` - so neither file ever appears in File Explorer, and `.reference-cache.json` is not used/updated. `"file"` in the printed JSON result is `null` in this mode since nothing was saved.

`speak.sh` (Welcome greeting) and `speak-news.sh` (News Flash), both defaulting to `jeremy-clarkson-this-is-brilliant.mp3` when no Voice Profile is active, wire this in already (`TTS_REFERENCE_AUDIO`/`TTS_REFERENCE_TEXT`/`TTS_DELIVERY_STYLE` env vars, plus `--ephemeral`), falling back to Piper automatically if the clone server errors or is unreachable - set `TTS_CLONE_ENABLED=0` to force Piper.

## From the MCC Page

Peter uploads a reference clip and generates narration directly at `/tts` - upload, type the script, adjust language/style/speed, generate. Every output is saved into `user_files` and shows up in File Explorer. (The page itself has no ephemeral toggle - that's only for scripted/automated callers via the CLI script above.)

## Voice Profiles (MCC Settings page)

`/settings` manages named, reusable voice configurations (`src/lib/server/voice-profiles.ts`, `~/mcc-data (OPENCLAW_MCC_DATA_ROOT)/voice-profiles.json`) - each one bundles a reference clip, its transcript, delivery style, content style, language, speed, and loudness target. Exactly one profile can be active at a time; activating one writes `~/mcc-data (OPENCLAW_MCC_DATA_ROOT)/active-voice-profile.json` (cleared if the active profile is deactivated or deleted).

**Delivery style vs. content style** - these are two different knobs, easy to conflate:
- **Delivery style** is a TTS synthesis parameter, passed straight through to Qwen3-TTS's `style` field (e.g. `"slow and clear, pause after each sentence"`). It shapes pacing/tone only and never changes the words being spoken.
- **Content style** is an optional persona/rewrite instruction (e.g. `"speak in Jeremy Clarkson crude humour"`). When set, `speak-news.sh` runs the input text through `rewrite_narration_style.py` (a local-LLM call, see `skills/smartthings-monitoring/scripts/rewrite_narration_style.py`) to actually rewrite the words into that persona *before* synthesis. Leave it blank to speak text as-is.

Reference clips are uploaded via `/api/voice-profiles/upload-reference` into `~/mcc-data (OPENCLAW_MCC_DATA_ROOT)/voice-profile-references/` - **not** `user_files`, deliberately: unlike a one-off `/tts` page narration, a profile is a durable, ongoing configuration, and `user_files` is a general-purpose pool anyone can delete from via File Explorer. The create/update routes reject a `referenceFileName` that doesn't actually exist there.

Both `speak-news.sh` (News Flash) and `speak.sh` (Welcome greeting) read `active-voice-profile.json` and use it as the *default* source for `TTS_REFERENCE_AUDIO`/`TEXT`/`STYLE`/`CONTENT_STYLE`/`LANGUAGE`/`SPEED`/`SPEECH_LOUDNORM_TARGET` - resolving `referenceFileName` to a local path under `VOICE_PROFILE_REFERENCES_ROOT` and passing it via `--reference-audio` (not `--reference-file`), so the ephemeral/nothing-saved-to-user_files guarantee still holds even with a profile active. If content style is set, the content-rewrite step runs first (see above), then synthesis proceeds with the (possibly rewritten) text. Any of those env vars set explicitly still overrides the active profile, same precedence as everything else in both scripts. Falls straight through to the hardcoded Jeremy Clarkson default if no profile is active or its reference clip has since been deleted - a greeting or a news flash always plays either way. The two scripts were kept deliberately out of sync on this until the Welcome greeting was aligned onto the same voice-profile pipeline as the News Flash (it still doesn't get the News Flash's background chime/bed mix or its saved-copy-in-user_files behavior, both of which are News Flash-specific).

The Settings page's "Test Sample" button (`/api/voice-profiles/[id]/sample`) previews a profile through the same clone server, loudness-normalized the same way `speak-news.sh` does, without touching the physical Echo speaker.

## OptiPlex Server Setup

One-time, on OptiPlex. Create the venv **outside `~/.openclaw`** - anything under `~/.openclaw/workspace` is scanned by `optiplex-capture-live-changes.sh` and can get pushed to Gitea as a "live change" (a venv with torch installed, ~5GB, actually happened this way on 2026-08-07 before that script excluded `venv/` - see its comments). `~/venvs/` keeps it structurally out of reach regardless of that script's exclusion list:

```bash
mkdir -p ~/venvs
python3 -m venv ~/venvs/tts-voice-clone-venv
~/venvs/tts-voice-clone-venv/bin/pip install -r ~/.openclaw/workspace/skills/tts-voice-clone/scripts/requirements.txt
```

Debian/Ubuntu's system Python is "externally managed" (PEP 668) and refuses `pip install` outside a venv - make sure to call the venv's own `pip` as above, not the bare `pip` on PATH, or it'll error with `externally-managed-environment`.

Then install `deploy/systemd/bud-qwen-tts.service` (same pattern as `mcc-visual-office.service` - see the mcc project's README "Systemd Service" section):

```bash
mkdir -p ~/.config/systemd/user
cp ~/src/bud/deploy/systemd/bud-qwen-tts.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user enable bud-qwen-tts
systemctl --user start bud-qwen-tts
```

First run downloads the ~2.5GB model from Hugging Face - subsequent starts are fast since it stays resident. Check it's up: `curl http://127.0.0.1:8790/health` (should report `"loaded":true` and `"whisperLoaded":true`).

Verified working end-to-end on 2026-08-07 (voice cloning, auto-transcription, speed control) - if `load_model()`/`run_inference()` in `serve_qwen3_tts.py` ever start erroring after a `qwen-tts` package version bump, check https://github.com/QwenLM/Qwen3-TTS's reference inference script for the current call signature.

## Why Qwen3-TTS over dots.tts

The task allowed either. Qwen3-TTS (0.6B, ~2.5GB, Apache 2.0) was chosen over dots.tts (rednote-hilab, 2B params) because dots.tts's only serving path is vLLM-Omni, which targets CUDA - a poor fit for CPU-only inference either way. Qwen3-TTS is smaller and lighter to run on the OptiPlex (i7, 16GB RAM, no GPU) while still doing 3-second zero-shot voice cloning.
