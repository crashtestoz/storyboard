# vendor/

`serve_qwen3_tts.py` is a copy of the Qwen3-TTS voice-clone server that lives
on OptiPlex at
`~/.openclaw/workspace/skills/tts-voice-clone/scripts/serve_qwen3_tts.py`,
with one change made here.

## The change: bfloat16 on Apple Silicon, not float16

The `mps` branch had never actually run — OptiPlex has no GPU, so it always
took the `cpu` path, and the comment in the original says as much. Exercised
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
OptiPlex itself, which has no GPU to take the branch.
