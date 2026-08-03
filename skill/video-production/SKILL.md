---
name: video-production
description: Build, edit, subtitle, inspect, prepare, and compress video through reusable FFmpeg, OpenCV, Pillow, and NumPy workflows. Use when Codex needs to create kinetic word-highlight subtitles, composite configurable wave or motion graphics, generate contact sheets to visually inspect a video, tone-map HDR/HLG footage to Rec.709, preserve aspect ratio through horizontal or vertical crops, verify exports, or create quality-based compressed deliverables.
---

# Video Production

Build video through a reproducible pipeline, keep intermediates separate from delivery files, and promote an export only after visual and technical QC.

For visually led work, use `$design` to establish the audience, experience, hierarchy, visual system, signature motif, typography, and motion purpose. Translate that contract into video-safe composition and timing with this skill. Use `references/tool-selection.md` to choose the smallest suitable production stack.

## Core rules

- Preserve source geometry. Crop to the destination shape first, then apply one uniform scale factor to both axes. Never squeeze a horizontal frame into a vertical or square box.
- Preserve resolution unless the user explicitly requests resizing.
- Replace camera audio only when requested. Map audio deliberately and verify the delivered stream.
- Use quality-based compression by default. Do not force a target size with bitrate, buffer, two-pass, or `-fs` constraints unless explicitly requested.
- Inspect rendered frames. Do not trust code or metadata alone.

## Tools

- `scripts/contact_sheet.py`: sample frames across a video into one timestamped image. Use a broad interval for whole-video structure, then a shorter interval around suspect sections.
- `scripts/kinetic_subtitles.py`: burn word-timed, active-word-highlighted captions from JSON. Read `references/subtitle-timings.md` for the schema.
- `scripts/motion_graphics.py`: composite JSON-driven waves, deterministic dots/sparkles, animated rectangles, circles, and route lines. Read `references/motion-graphics.md` before adapting an effect.
- `scripts/prepare_hdr_sdr.sh`: create a 10-bit Rec.709 ProRes mezzanine from HLG/BT.2020 footage with a Hable tone map.
- `scripts/compress_video.sh`: make a resolution-preserving, quality-based sharing copy. Its default `auto` mode probes and prefers the saved Intel AV1 QSV profile (`global_quality 30`, `preset slow`, look-ahead depth 80, AAC 64 kbps), then falls back to slow quality-based H.264 only when that exact hardware profile cannot run. Explicit `libx264`, `libsvtav1`, and `av1_qsv` modes remain available.
- `scripts/verify_video.sh`: print delivery metadata and fully decode video and audio to catch corruption.

For ordinary compression, run the default command without choosing an encoder:

```bash
scripts/compress_video.sh INPUT OUTPUT
```

Only pass the optional encoder argument when the user has a special codec, compatibility, or delivery requirement. Do not tune the saved quality profile by default.

Python tools require OpenCV, Pillow, and NumPy. If unavailable, create a project-local environment with `uv venv` and install `opencv-python-headless pillow numpy`; do not mutate the system Python environment.

Start Python production environments with `uv`, and install only the selected stack:

```bash
uv venv --python 3.11
uv pip install --python .venv/bin/python opencv-python-headless pillow numpy
```

## Workflow

1. Inspect source metadata and color space with `ffprobe`.
2. Tone-map HDR source only when the delivery pipeline is SDR.
3. Establish exact audio and lyric anchors before effects work.
4. Render motion graphics and subtitles from data-driven timing.
5. Generate a whole-video contact sheet and focused dense sheets for risky sections.
6. Render one high-quality master or mezzanine, then derive delivery copies from it.
7. Run `verify_video.sh`, inspect the actual delivery encode, and report duration, frame count, resolution, codecs, and natural file size.

Keep user-supplied input and output paths explicit. Never hard-code a project-specific filename or overwrite an input.
