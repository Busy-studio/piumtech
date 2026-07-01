# v8.5.3.64.6.4 Transient / HF Ownership Lock

## Purpose

This patch consolidates overlapping BAMix transient and side-HF processing so the same micro-transient area is not repeatedly emphasized inside the block-wise stem premaster render.

It is built directly on the available v64.5 deterministic quality codebase and keeps the single full premaster render policy.

## Changes

- Adds adaptive BAMix render block sizing.
  - Default auto mode uses song duration, stem count, SMR stem count, memory limit, and CPU limit.
  - A stale `BUSY_AUTOMIX_RENDER_BLOCK_SIZE=16384` is ignored in auto mode unless hard lock is enabled.
  - 2 CPU / 8 GB / normal 3 minute stem jobs typically select `131072` samples.
- Adds block seam smoothing.
  - Main premaster output seam smoother.
  - SMR stem-cache seam smoother.
- Adds transient/HF ownership coordination.
  - `drum_parallel_density` owns drum body/transient density when active.
  - `transient_ghost` is reduced to an assist role when both are active.
  - `side_texture_control` owns side-HF hash cleanup when active.
  - ERB M/S resonance suppression is reduced to a broader resonance assist role when side texture is already active.
- Adds assist delta consolidation.
  - Measures the added assist delta, not the dry stem sum.
  - If the added delta exceeds the HF micro-transient budget, only the assist delta is scaled.

## Important Non-Changes

- No extra full-length candidate render.
- No correction rerender fallback.
- No DAW reference comparison.
- No phase-inversion cancellation.
- No blind BWE, vocoder, diffusion restoration, or generated audio.
- No final limiter or maximizer added in BAMix.

## Key Env Controls

```text
BUSY_AUTOMIX_RENDER_BLOCK_SIZE_AUTO=1
BUSY_AUTOMIX_RENDER_BLOCK_SIZE_HARD_LOCK=0
BUSY_AUTOMIX_MEMORY_LIMIT_MB=8192
BUSY_AUTOMIX_CPU_LIMIT=2

BUSY_BAMIX_V6463_SEAM_SMOOTH_MS=2.0
BUSY_BAMIX_V6464_TRANSIENT_HF_OWNERSHIP_LOCK=1
BUSY_BAMIX_V6464_TRANSIENT_GHOST_ASSIST_SCALE=0.34
BUSY_BAMIX_V6464_ERB_AFTER_SIDE_TEXTURE_SCALE=0.42
BUSY_BAMIX_V6464_ASSIST_DELTA_CONSOLIDATOR=1
```

To force a fixed legacy block size:

```text
BUSY_AUTOMIX_RENDER_BLOCK_SIZE_HARD_LOCK=1
BUSY_AUTOMIX_RENDER_BLOCK_SIZE=16384
```

## Debug Telemetry

The result report/debug brief exposes:

```text
v6463_adaptive_block_sizing
v6464_transient_hf_ownership_lock
v6464_assist_delta_consolidator
v6463_output_seam_smoother
```

