---
name: compress-video
description: Compress video files with quality-based encoding while preserving resolution and avoiding forced file-size targets. Use when Codex needs to make a video smaller, create a high-quality sharing copy, use Intel QSV AV1, or inspect and verify a compressed video deliverable.
---

# Compress Video

Create a smaller copy through constant-quality encoding. Preserve the source resolution unless the user explicitly asks to resize it.

## Workflow

1. Resolve explicit input and output paths. Never overwrite the input, and never hard-code a project-specific output name or directory.
2. Inspect the input with `ffprobe`, including duration, dimensions, frame rate, codecs, and size.
3. Prefer Intel `av1_qsv` when the user wants efficient AV1 and compatible hardware is available.
4. Run `scripts/compress_video.sh INPUT OUTPUT` for the saved QSV profile.
5. Report the naturally produced size. Do not chase a requested size by adding video bitrate, maximum-rate, buffer-size, two-pass, or `-fs` constraints unless the user explicitly requests size targeting.
6. Verify the output with `ffprobe` and a complete decode before delivery.

## Saved quality profile

The bundled script keeps the input dimensions and uses:

- `av1_qsv`
- `-global_quality 30`
- `-preset slow`
- `-look_ahead 1`
- `-look_ahead_depth 80`
- AAC audio at 64 kbps
- MP4 fast-start metadata

If QSV initialization fails, stop and report the hardware/driver error. Use a software AV1 fallback only when the user accepts the slower encode or has asked for the best available alternative.

Do not treat a target size as an exact requirement when the user asked for quality-based output. A result below a stated maximum is acceptable even when it does not land near the preferred size.
