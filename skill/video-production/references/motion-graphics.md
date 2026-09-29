# Motion graphics and visual QC

## Animated wave

The wave is a time-varying polygon mask:

1. Compute a smooth fade envelope for the effect interval.
2. Sample a sine curve across the frame width.
3. Close the curve to the bottom edge and fill it as a mask.
4. Composite either a solid color or an aspect-safe crop of another video through that mask.
5. Draw two thin offset edge lines for separation and brand color.

`motion_graphics.py` exposes the important visual controls: baseline, amplitude, cycles, speed, opacity, fill, and timing. A baseline of `0.805` places the crest around lower-body level; larger values move it down. Inspect hands and faces throughout the interval before approval.

## Graphics configuration

Pass a JSON array or an object containing `effects`. Supported types are `wave`, `particles`, `rectangle`, `circle`, and `route`. Every effect requires `start` and `end` in timeline seconds and may set `fade` and `opacity`.

```json
{
  "effects": [
    {
      "type": "wave",
      "start": 11.7,
      "end": 17.8,
      "baseline": 0.805,
      "amplitude": 0.008,
      "fill_color": "#2F9C95"
    },
    {
      "type": "particles",
      "start": 66.8,
      "end": 72.4,
      "count": 42,
      "seed": 61,
      "colors": ["#EF3340", "#F7F3E8"]
    },
    {
      "type": "rectangle",
      "start": 18.0,
      "end": 21.5,
      "x": 0.70,
      "y": 0.06,
      "width": 0.25,
      "height": 0.16
    },
    {
      "type": "circle",
      "start": 22.3,
      "end": 31.3,
      "x": 0.90,
      "y": 0.16,
      "radius": 0.105
    },
    {
      "type": "route",
      "start": 59.6,
      "end": 66.9,
      "points": [[0.06, 0.84], [0.30, 0.74], [0.55, 0.60], [0.94, 0.48]]
    }
  ]
}
```

All coordinates and sizes are normalized fractions of the frame, so the same design scales to different resolutions. Keep seeded particles deterministic so reviews and rerenders match exactly.

## Contact-sheet inspection

Use `contact_sheet.py` to approximate watching the edit in a static visual:

```bash
python scripts/contact_sheet.py input.mp4 whole.jpg --interval 4 --columns 5
python scripts/contact_sheet.py input.mp4 detail.jpg --start 11 --duration 8 --interval 0.5 --columns 4
```

The broad sheet reveals pacing, framing changes, inconsistent crops, and large occlusions. The dense sheet reveals effect motion, entrances, exits, and face clearance. Always inspect individual full-resolution frames after the sheet identifies a risky moment.

## Aspect-safe panels

Use `resize_cover` to fill a horizontal, square, or vertical panel by cropping first and scaling uniformly. Use `resize_contain` when every source pixel must remain visible and letterboxing is acceptable. Never resize the complete source independently to a mismatched width and height.
