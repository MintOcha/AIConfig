# Video tool selection

Choose one primary composition layer and keep FFmpeg as the codec, muxing, audio, and verification foundation. Do not install every library by default.

## Visual direction

Use `$design` before implementation when typography, color, hierarchy, layout, or a signature visual motif materially affects the result. Define a compact visual contract, then translate it into title-safe margins, caption hierarchy, palette, shape language, transitions, and purposeful motion. Inspect rendered frames at delivery resolution.

## Environment

Use a project-local `uv` environment. Python 3.11 is compatible with the tools below at the time of this research.

```bash
uv venv --python 3.11
uv pip install --python .venv/bin/python PACKAGE
```

## Selection matrix

### FFmpeg and the bundled scripts

Use for deterministic trimming, audio replacement, codec/color conversion, muxing, subtitles, the provided motion primitives, contact sheets, and final verification. Prefer this path when the desired operation is already expressed by a bundled script or a concise FFmpeg filtergraph.

### MoviePy v2

Use for conventional clip timelines, cuts, concatenation, titles, image/text overlays, audio, and straightforward compositing when a Python object model is more readable than a filtergraph.

```bash
uv pip install --python .venv/bin/python moviepy
```

Use current v2 syntax. Import from `moviepy`, not the removed `moviepy.editor` namespace. Clip mutations generally use `with_*`, effects are classes applied with `with_effects()`, and operations use names such as `resized`, `cropped`, and `rotated`. MoviePy v2 deliberately reduced dependencies and centers complex image work on Pillow.

Official documentation: <https://zulko.github.io/moviepy/index.html>

### Movis

Use for authored motion design: layer compositions, sub-pixel transforms, keyframes and easing, blend modes, nested compositions, multiple text outlines, custom NumPy effects, and cached half/quarter-resolution previews. It is the strongest fit of these tools for complex graphics that resemble a motion-design application.

```bash
uv pip install --python .venv/bin/python movis
```

Movis layers and custom effects can return RGBA NumPy arrays for a given timeline time, which pairs naturally with custom procedural graphics or ML-generated masks. Its published documentation states Python 3.9–3.11 support; use Python 3.11 unless current package metadata says otherwise.

Repository and documentation: <https://github.com/rezoo/movis> and <https://rezoo.github.io/movis/>

### VideoPython

Use for LLM- or schema-driven edit plans, bounded-memory streaming edits, automatic segment normalization, scene/audio analysis, auto-editing, MCP integration, or optional local AI generation/dubbing workflows.

```bash
uv pip install --python .venv/bin/python videopython
```

Install `videopython[ai]` or `videopython[ai,mcp]` only when the requested task needs those heavy local-AI features and suitable GPU resources exist. The core package advertises Python 3.11 through 3.13, structured plan validation, and approximately O(1) frame memory for streamed edits.

Official site: <https://videopython.com/>

### OpenCV, Pillow, and NumPy

Use directly for unusual per-frame effects, masks, geometry, procedural graphics, and tightly controlled subtitle rendering. When two or more scripts need the same geometry or encoder behavior, centralize it in `scripts/video_tools.py` rather than copying helpers.

## Decision order

1. Reuse a bundled deterministic script.
2. Use FFmpeg directly for a concise codec/filter/audio operation.
3. Use MoviePy for ordinary Python clip composition.
4. Use Movis when keyframed motion design and layered effects are the main problem.
5. Use VideoPython when structured agent plans, analysis, auto-editing, or local AI are the main problem.
6. Write custom OpenCV/NumPy code only for behavior the chosen library does not express cleanly.

Do not combine multiple high-level composition libraries without a concrete need. Export a clean mezzanine between systems when integration is unavoidable.
