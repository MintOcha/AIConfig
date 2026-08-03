#!/usr/bin/env python3
"""Burn word-timed kinetic subtitles into a video."""

from __future__ import annotations

import argparse
import json
from dataclasses import dataclass
from pathlib import Path

import cv2
import numpy as np
from PIL import Image, ImageDraw, ImageFont

from video_tools import envelope, finish_encoder, open_video, start_encoder


@dataclass(frozen=True)
class Word:
    text: str
    start: float
    end: float


@dataclass(frozen=True)
class Caption:
    start: float
    end: float
    words: tuple[Word, ...]


def load_captions(path: Path) -> tuple[Caption, ...]:
    data = json.loads(path.read_text(encoding="utf-8"))
    rows = data["captions"] if isinstance(data, dict) else data
    captions = []
    for row in rows:
        words = tuple(Word(str(word["text"]), float(word["start"]), float(word["end"])) for word in row["words"])
        caption = Caption(float(row["start"]), float(row["end"]), words)
        if caption.start < 0 or caption.end <= caption.start or not words:
            raise ValueError(f"Invalid caption: {row}")
        if any(word.start < caption.start or word.end > caption.end or word.end <= word.start for word in words):
            raise ValueError(f"Word timing falls outside caption: {row}")
        captions.append(caption)
    if not captions:
        raise ValueError("At least one caption is required")
    return tuple(sorted(captions, key=lambda caption: caption.start))


def color(value: str) -> tuple[int, int, int]:
    value = value.removeprefix("#")
    if len(value) != 6:
        raise ValueError(f"Expected RRGGBB color, got: {value}")
    return tuple(int(value[index : index + 2], 16) for index in (0, 2, 4))


def render_caption(
    frame: np.ndarray,
    time_seconds: float,
    caption: Caption,
    font: ImageFont.FreeTypeFont,
    text_color: tuple[int, int, int],
    highlight_color: tuple[int, int, int],
    panel_color: tuple[int, int, int],
) -> np.ndarray:
    fade = envelope(time_seconds, caption.start - 0.10, caption.end + 0.26, 0.20)
    active = next((index for index, word in enumerate(caption.words) if word.start <= time_seconds <= word.end), -1)
    image = Image.fromarray(cv2.cvtColor(frame, cv2.COLOR_BGR2RGB)).convert("RGBA")
    layer = Image.new("RGBA", image.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(layer)
    max_width = image.width * 0.82
    space = draw.textlength(" ", font=font)
    lines: list[list[tuple[int, str]]] = [[]]
    widths = [0.0]
    for index, word in enumerate(caption.words):
        word_width = draw.textlength(word.text, font=font)
        proposed = widths[-1] + (space if lines[-1] else 0) + word_width
        if proposed > max_width and lines[-1]:
            lines.append([])
            widths.append(0.0)
        if lines[-1]:
            widths[-1] += space
        lines[-1].append((index, word.text))
        widths[-1] += word_width
    line_height = round(font.size * 1.20)
    total_height = line_height * len(lines)
    y0 = image.height - total_height - round(image.height * 0.065)
    panel_x0 = round(image.width / 2 - max(widths) / 2 - font.size * 0.55)
    panel_x1 = round(image.width / 2 + max(widths) / 2 + font.size * 0.55)
    draw.rounded_rectangle(
        (panel_x0, y0 - font.size * 0.28, panel_x1, y0 + total_height + font.size * 0.12),
        radius=round(font.size * 0.32),
        fill=(*panel_color, round(175 * fade)),
    )
    for line_index, line in enumerate(lines):
        x = image.width / 2 - widths[line_index] / 2
        y = y0 + line_index * line_height
        for word_index, text in line:
            fill = highlight_color if word_index == active else text_color
            draw.text(
                (x, y), text, font=font, fill=(*fill, round(255 * fade)),
                stroke_width=max(1, font.size // 24), stroke_fill=(*panel_color, round(245 * fade)),
            )
            x += draw.textlength(text, font=font) + space
    image.alpha_composite(layer)
    return cv2.cvtColor(np.asarray(image.convert("RGB")), cv2.COLOR_RGB2BGR)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("timings", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--font", default="DejaVuSans-Bold.ttf")
    parser.add_argument("--font-scale", type=float, default=0.046)
    parser.add_argument("--text-color", default="#F7F3E8")
    parser.add_argument("--highlight-color", default="#EF3340")
    parser.add_argument("--panel-color", default="#14213D")
    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument("--preset", default="slow")
    args = parser.parse_args()

    captions = load_captions(args.timings)
    capture, info = open_video(args.input)
    font = ImageFont.truetype(args.font, max(16, round(info.height * args.font_scale)))
    encoder = start_encoder(args.input, args.output, info, args.crf, args.preset, "192k")
    caption_index = 0
    try:
        for frame_number in range(info.frame_count):
            ok, frame = capture.read()
            if not ok:
                raise RuntimeError(f"Input ended at frame {frame_number}")
            time_seconds = frame_number / info.fps
            while caption_index + 1 < len(captions) and time_seconds > captions[caption_index].end + 0.26:
                caption_index += 1
            caption = captions[caption_index]
            if caption.start - 0.10 <= time_seconds <= caption.end + 0.26:
                frame = render_caption(
                    frame, time_seconds, caption, font,
                    color(args.text_color), color(args.highlight_color), color(args.panel_color),
                )
            if encoder.stdin is None:
                raise RuntimeError("FFmpeg input pipe is unavailable")
            encoder.stdin.write(frame.tobytes())
    finally:
        capture.release()
        finish_encoder(encoder)


if __name__ == "__main__":
    main()
