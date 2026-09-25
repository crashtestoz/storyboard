# vendor/

## Running the Qwen3-TTS voice-clone server

Storyboard's **Qwen3-TTS voice clone** speech engine (`qwen3-clone` in
`tts-services.json`) talks to this server at `http://127.0.0.1:8790`. Start it
in its own Terminal tab and leave it running while you use Storyboard:

```sh
vendor/start-qwen3-tts.sh
```

The first run creates `vendor/.venv` (gitignored) and installs
`requirements.txt`, which takes a few minutes; the first launch then downloads
the ~2.5 GB `Qwen/Qwen3-TTS-12Hz-0.6B-Base` model and a small Whisper model
(for transcribing reference clips) into the Hugging Face cache. After that it
starts in well under a minute. Any `serve_qwen3_tts.py` option passes through,
e.g. `vendor/start-qwen3-tts.sh --port 8791`. Needs Python 3.10+
(`brew install python@3.14`); on Apple Silicon it runs on the GPU in bfloat16
(see below).


`serve_qwen3_tts.py` is a copy of the Qwen3-TTS voice-clone server that
normally lives alongside a separate TTS-hosting project's own
`tts-voice-clone` skill, with one change made here.

## The change: bfloat16 on Apple Silicon, not float16

The `mps` branch had never actually run — the CPU-only host it was written
for always took the `cpu` path, and the comment in the original says as
much. Exercised
for the first time on a Mac mini M4 Pro, `mps` + `float16` failed every
request with:

    probability tensor contains either `inf`, `nan` or element < 0

which is the *same* failure the original's own comment records for float16 on
CPU, with the same cause: float16's exponent range is too narrow for the
sampling step.

The original resolved that on CPU by trying bfloat16 (correct, but slow on an
i7-8700T with no native bf16) and settling on float32. On Apple Silicon the
objection does not apply — bf16 is native on the GPU — so `mps` now uses
`bfloat16`, matching what the `cuda` branch already chose for the same reason.

Measured after the change: 2.5s of speech in 6.6s wall, against the CPU path's
~15s-and-counting for a comparable line.

`--device` / `--dtype` were added to override the automatic choice.

**This fix is worth carrying back upstream**, though it changes nothing on
the original CPU-only host, which has no GPU to take the branch.
