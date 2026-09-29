#!/usr/bin/env python3
"""Composite data-driven motion graphics over a video."""

from __future__ import annotations

import argparse
import json
import math
from pathlib import Path
from typing import Any

import cv2
import numpy as np

from video_tools import blend, clamp, envelope, finish_encoder, open_video, smoothstep, start_encoder


def bgr(value: str) -> tuple[int, int, int]:
    value = value.removeprefix("#")
    if len(value) != 6:
        raise ValueError(f"Expected RRGGBB color, got: {value}")
    red, green, blue = (int(value[index : index + 2], 16) for index in (0, 2, 4))
    return blue, green, red


def effect_amount(effect: dict[str, Any], time_seconds: float) -> float:
    start = float(effect["start"])
    end = float(effect["end"])
    fade = float(effect.get("fade", min(0.35, max(0.05, (end - start) / 4))))
    return envelope(time_seconds, start, end, fade) * float(effect.get("opacity", 1.0))


def wave(frame: np.ndarray, effect: dict[str, Any], time_seconds: float, amount: float) -> np.ndarray:
    height, width = frame.shape[:2]
    baseline = float(effect.get("baseline", 0.805))
    amplitude = float(effect.get("amplitude", 0.008))
    cycles = float(effect.get("cycles", 3.5))
    speed = float(effect.get("speed", 2.8))
    edge = []
    polygon = [(0, height)]
    for x in range(0, width + 12, 12):
        phase = math.tau * cycles * x / width + time_seconds * speed
        y = round(height * (baseline + math.sin(phase) * amplitude))
        edge.append((x, y))
        polygon.append((x, y))
    polygon.append((width, height))
    mask = np.zeros((height, width), dtype=np.float32)
    cv2.fillPoly(mask, [np.asarray(polygon, np.int32)], amount)
    fill = np.full_like(frame, bgr(effect.get("fill_color", "#2F9C95")))
    result = blend(frame, fill, mask)
    lines = result.copy()
    cv2.polylines(lines, [np.asarray(edge, np.int32)], False, bgr(effect.get("line_color", "#F7F3E8")), max(2, height // 180), cv2.LINE_AA)
    offset = round(height * float(effect.get("line_offset", 0.010)))
    cv2.polylines(lines, [np.asarray([(x, y - offset) for x, y in edge], np.int32)], False, bgr(effect.get("accent_color", "#EF3340")), max(1, height // 300), cv2.LINE_AA)
    return blend(result, lines, amount)


def particles(frame: np.ndarray, effect: dict[str, Any], time_seconds: float, amount: float) -> np.ndarray:
    height, width = frame.shape[:2]
    overlay = frame.copy()
    count = int(effect.get("count", 42))
    seed = int(effect.get("seed", 61))
    start = float(effect["start"])
    colors = tuple(bgr(value) for value in effect.get("colors", ["#EF3340", "#F7F3E8"]))
    rng = np.random.default_rng(seed)
    for index in range(count):
        x_seed, y_seed, speed_seed, size_seed = rng.random(4)
        x = round(x_seed * width)
        speed = height * (0.020 + speed_seed * 0.035)
        y = round((y_seed * height - (time_seconds - start) * speed) % height)
        radius = max(2, round(height * (0.003 + size_seed * 0.006)))
        color = colors[index % len(colors)]
        if index % 4 == 0:
            cv2.line(overlay, (x - radius, y), (x + radius, y), color, max(1, radius // 2), cv2.LINE_AA)
            cv2.line(overlay, (x, y - radius), (x, y + radius), color, max(1, radius // 2), cv2.LINE_AA)
        else:
            cv2.circle(overlay, (x, y), radius, color, -1, cv2.LINE_AA)
    return blend(frame, overlay, amount)


def rectangle(frame: np.ndarray, effect: dict[str, Any], time_seconds: float, amount: float) -> np.ndarray:
    height, width = frame.shape[:2]
    x = round(width * float(effect.get("x", 0.70)))
    y = round(height * float(effect.get("y", 0.06)))
    full_width = round(width * float(effect.get("width", 0.25)))
    box_height = round(height * float(effect.get("height", 0.16)))
    reveal_duration = max(0.01, float(effect.get("reveal_duration", 0.5)))
    progress = smoothstep((time_seconds - float(effect["start"])) / reveal_duration)
    box_width = max(1, round(full_width * progress))
    overlay = frame.copy()
    cv2.rectangle(overlay, (x, y), (x + box_width, y + box_height), bgr(effect.get("fill_color", "#EF3340")), -1, cv2.LINE_AA)
    border_width = max(1, round(height * float(effect.get("border_width", 0.004))))
    cv2.rectangle(overlay, (x, y), (x + box_width, y + box_height), bgr(effect.get("border_color", "#F7F3E8")), border_width, cv2.LINE_AA)
    return blend(frame, overlay, amount)


def circle(frame: np.ndarray, effect: dict[str, Any], time_seconds: float, amount: float) -> np.ndarray:
    height, width = frame.shape[:2]
    center = (round(width * float(effect.get("x", 0.90))), round(height * float(effect.get("y", 0.16))))
    reveal_duration = max(0.01, float(effect.get("reveal_duration", 0.45)))
    progress = smoothstep((time_seconds - float(effect["start"])) / reveal_duration)
    radius = max(1, round(height * float(effect.get("radius", 0.105)) * progress))
    overlay = frame.copy()
    cv2.circle(overlay, center, radius, bgr(effect.get("fill_color", "#2F9C95")), -1, cv2.LINE_AA)
    cv2.circle(overlay, center, radius, bgr(effect.get("border_color", "#F7F3E8")), max(2, height // 180), cv2.LINE_AA)
    cv2.circle(overlay, center, radius + max(3, height // 180), bgr(effect.get("accent_color", "#EF3340")), max(1, height // 300), cv2.LINE_AA)
    return blend(frame, overlay, amount)


def route(frame: np.ndarray, effect: dict[str, Any], time_seconds: float, amount: float) -> np.ndarray:
    height, width = frame.shape[:2]
    raw_points = effect.get("points")
    if not raw_points or len(raw_points) < 2:
        raise ValueError("A route effect requires at least two normalized points")
    points = [(round(width * float(x)), round(height * float(y))) for x, y in raw_points]
    duration = float(effect["end"]) - float(effect["start"])
    progress = smoothstep((time_seconds - float(effect["start"])) / duration)
    visible = max(2, round(len(points) * clamp(progress)))
    path = np.asarray(points[:visible], np.int32)
    overlay = frame.copy()
    base_width = max(2, round(height * float(effect.get("width", 0.012))))
    cv2.polylines(overlay, [path], False, bgr(effect.get("under_color", "#F7F3E8")), base_width + max(2, height // 180), cv2.LINE_AA)
    cv2.polylines(overlay, [path], False, bgr(effect.get("color", "#EF3340")), base_width, cv2.LINE_AA)
    return blend(frame, overlay, amount)


RENDERERS = {
    "wave": wave,
    "particles": particles,
    "rectangle": rectangle,
    "circle": circle,
    "route": route,
}


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("input", type=Path)
    parser.add_argument("config", type=Path)
    parser.add_argument("output", type=Path)
    parser.add_argument("--crf", type=int, default=18)
    parser.add_argument("--preset", default="slow")
    args = parser.parse_args()

    payload = json.loads(args.config.read_text(encoding="utf-8"))
    effects = payload["effects"] if isinstance(payload, dict) else payload
    for effect in effects:
        if effect.get("type") not in RENDERERS:
            raise ValueError(f"Unsupported effect type: {effect.get('type')}")
        if float(effect["start"]) < 0 or float(effect["end"]) <= float(effect["start"]):
            raise ValueError(f"Invalid effect interval: {effect}")

    capture, info = open_video(args.input)
    encoder = start_encoder(args.input, args.output, info, args.crf, args.preset, "192k")
    try:
        for frame_number in range(info.frame_count):
            ok, frame = capture.read()
            if not ok:
                raise RuntimeError(f"Input ended at frame {frame_number}")
            time_seconds = frame_number / info.fps
            for effect in effects:
                amount = effect_amount(effect, time_seconds)
                if amount > 0:
                    frame = RENDERERS[effect["type"]](frame, effect, time_seconds, amount)
            if encoder.stdin is None:
                raise RuntimeError("FFmpeg input pipe is unavailable")
            encoder.stdin.write(frame.tobytes())
    finally:
        capture.release()
        finish_encoder(encoder)


if __name__ == "__main__":
    main()
