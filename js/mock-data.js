/* ==========================================================================
   Mock project state.
   --------------------------------------------------------------------------
   POC ONLY — there is no backend. This stands in for what the orchestrator
   would hold and what a GET /project would return.

   Content is the real Falcon test project this tool was designed against,
   and the thumbnails in assets/thumbs/ are real MiniMax H3 output frames,
   so the layout is being exercised at realistic text lengths and aspect
   ratios rather than lorem ipsum.

   `simulate` on each shot scripts what the fake render does when you press
   Render All. It exists to demonstrate the stability/error-correction states
   from the design doc — every one of these outcomes is a failure mode that
   actually happened while driving vpipe by hand:
     ok          — clean run
     empty       — exit 0, WARN, no files written (the `frames: 5` bug)
     fast        — finished in a fraction of expected time (the malformed
                   Ref2VA reference-shape bug)
   A shot chained to a failed shot resolves to `blocked` on its own.
   ========================================================================== */

const MOCK_PROJECT = {
  name: "Falcon — ocean low pass",
  sceneDescription:
    "The Millennium Falcon, battered and weathered, sunlight glinting off " +
    "scuffed hull plating. Open ocean at golden hour, warm low sun, distant " +
    "clouds stacked on the horizon. Photorealistic, cinematic, shallow depth " +
    "of field.",
  styleRefs: [
    { id: "sr1", label: "falcon front", src: "assets/thumbs/shot-0019.jpg" },
    { id: "sr2", label: "falcon rear", src: "assets/thumbs/shot-0035.jpg" },
  ],
  defaults: {
    model: "fl2va",
    resolution: "960x544",
    frames: 124,
    steps: 8,
  },
  shots: [
    {
      id: "s1",
      title: "Establishing — Falcon breaks cloud cover",
      prompt:
        "A high wide shot as the ship drops out of low cloud toward the water, " +
        "nose pitching down, engines flaring. Distant horizon, hazy light.",
      startRef: null,
      endRef: null,
      chainFromPrev: false,
      model: "fl2va",
      resolution: "960x544",
      frames: 124,
      steps: 8,
      seed: 6,
      status: "done",
      progress: 100,
      thumb: "assets/thumbs/shot-0002.jpg",
      runtime: "27m 44s",
      expected: "~28m",
      outputs: ["shot-1.mp4", "frames/ (124 png)"],
      simulate: "ok",
    },
    {
      id: "s2",
      title: "Low pass, spray peeling off the hull",
      prompt:
        "A low, fast tracking shot skimming just above the open ocean, following " +
        "the ship as it banks hard and streaks across the water, hull throwing a " +
        "long rippling reflection while sea spray kicks up in its wake. Deep " +
        "engine roar, rushing wind, the hiss of spray.",
      startRef: { kind: "chain", from: "s1", label: "last frame of shot 1" },
      endRef: null,
      chainFromPrev: true,
      model: "fl2va",
      resolution: "960x544",
      frames: 124,
      steps: 8,
      seed: 6,
      status: "draft",
      progress: 0,
      thumb: "assets/thumbs/shot-0019.jpg",
      runtime: null,
      expected: "~28m",
      outputs: [],
      simulate: "ok",
    },
    {
      id: "s3",
      title: "Reverse angle — ship fills frame, passes camera",
      prompt:
        "Reverse low angle from just above the wave tops as the ship roars past " +
        "camera left to right, wash flattening the water beneath it. Sub-bass " +
        "thrum as it passes.",
      startRef: { kind: "upload", label: "falcon-rear.png", src: "assets/thumbs/shot-0035.jpg" },
      endRef: null,
      chainFromPrev: false,
      model: "ref2va",
      resolution: "960x544",
      frames: 124,
      steps: 8,
      seed: 6,
      status: "draft",
      progress: 0,
      thumb: "assets/thumbs/shot-0035.jpg",
      runtime: null,
      expected: "~28m",
      outputs: [],
      simulate: "fast",
    },
    {
      id: "s4",
      title: "Pull out to wide, ship recedes toward the sun",
      prompt:
        "Camera pulls back and up as the ship recedes toward the low sun, wake " +
        "settling behind it, water going gold. Engine note falling away.",
      startRef: { kind: "chain", from: "s3", label: "last frame of shot 3" },
      endRef: null,
      chainFromPrev: true,
      model: "fl2va",
      resolution: "960x544",
      frames: 124,
      steps: 8,
      seed: 12,
      status: "draft",
      progress: 0,
      thumb: null,
      runtime: null,
      expected: "~28m",
      outputs: [],
      simulate: "ok",
    },
  ],
};

/* Scripted log output per outcome — modelled on real vpipe stdout, including
   the benign wired-pool warning that must NOT be treated as a failure. */
const MOCK_LOGS = {
  head: [
    ["INFO", "model registry: 'local/MiniMax-H3-FL2VA-8bit' -> models/local/MiniMax-H3-FL2VA-8bit"],
    ["INFO", "memory landscape at launch: 49152 MB RAM -- 37336 MB idle, 4115 MB wired"],
    ["NORMAL", "MetalMiniMaxH3Transformer: baked AdaLN for 7 steps (20 rows)"],
    ["INFO", "GenerateVideoStage: MiniMax-H3 (video+audio) at 960x544x124 frames, 8 steps"],
  ],
  benignWarn: [
    ["WARN", "wired pool: the box refused to wire 0 MB (errno 12) -- the rest of this run's weights stay reclaimable"],
    ["INFO", "classified: benign (graceful degradation to streamable memory) -- not a failure"],
  ],
  ok: [
    ["PROGRESS", "100% of 'denoise' completed (400/400)"],
    ["PROGRESS", "100% of 'vae decode' completed (105/105)"],
    ["OK", "output manifest verified: shot.mp4 (1.4 MB), 124/124 frames written"],
    ["OK", "runtime 27m 44s vs expected ~28m -- within tolerance"],
  ],
  empty: [
    ["PROGRESS", "99% of 'denoise' completed (347/350)"],
    ["WARN", "VaeDecodeStage: MiniMax-H3 video decode failed (only 2 latent frames: fewer than the 8 one chunk needs); skipping"],
    ["ERROR", "output manifest FAILED: shot.mp4 missing, 0/124 frames written (process exit code 0)"],
  ],
  fast: [
    ["PROGRESS", "66% of 'encoding references' completed (2/3)"],
    ["WARN", "GenerateVideoStage: the reference rows are not [n, 96] / [n, 32]; skipping"],
    ["ERROR", "no 'denoise' progress ever reported -- generation never started"],
    ["ERROR", "runtime 1m 25s vs expected ~28m (5%) -- flagged as silent failure despite exit code 0"],
  ],
  blocked: [
    ["WARN", "held: start reference resolves to shot 3's last frame, which was never written"],
    ["INFO", "not queued -- dependency unmet. Fix shot 3 and re-run to release this shot."],
  ],
};
